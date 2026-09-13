from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from credit_harness.memory.retrieval import current_signature, StructuredExperienceReranker
from credit_harness.memory.guidance import InvestigationGuidanceService
from .models import DocumentType, RetrievalTelemetry, RetrievalError, MANDATORY_SAFETY
from .projection import incident_query, embedding_text
from .embedding import normalize
from .rerank import rerank


@dataclass(frozen=True)
class HybridSelection:
    skills: tuple
    experiences: tuple


class HybridRetrievalService:
    """Knowledge suggestion only. No mutation-capable runtime is a dependency."""
    mandatory_safety = MANDATORY_SAFETY

    def __init__(self, repository, provider, *, telemetry_sink=None):
        self.repository, self.provider = repository, provider
        self.sources = repository.sources
        self.telemetry_sink = telemetry_sink
        self._telemetry = ContextVar("hybrid_retrieval_telemetry", default=RetrievalTelemetry())
        self._telemetry_revision = ContextVar("hybrid_telemetry_revision", default=0)

    @property
    def last_telemetry(self):
        return self._telemetry.get()

    @property
    def telemetry_revision(self):
        return self._telemetry_revision.get()

    def _emit(self, values):
        telemetry = RetrievalTelemetry(**values)
        self._telemetry.set(telemetry)
        self._telemetry_revision.set(self._telemetry_revision.get() + 1)
        if self.telemetry_sink:
            try:
                self.telemetry_sink(telemetry)
            except Exception:
                pass  # Telemetry cannot become a new runtime failure mode.

    def select(self, snapshot):
        start = perf_counter()
        telemetry = dict(embedding_space=None, hard_filter_candidate_count=0, vector_candidate_count=0,
            reranked_count=0, vector_latency_ms=0., rerank_latency_ms=0.)
        try:
            signature = current_signature(snapshot)
            scope = self.sources.query_scope(snapshot)
            space = self.repository.active_space()
            self.repository.validate_provider(space, self.provider)
            telemetry["embedding_space"] = space.space_id
            # Query vectors are ephemeral; no table or audit accepts them.
            vector = normalize(self.provider.embed_query(embedding_text(incident_query(snapshot))), space.dimension)
            selected = {}
            for kind in DocumentType:
                t = perf_counter()
                candidates, count = self.repository.search(kind, space, scope, snapshot.case_id, vector)
                telemetry["vector_latency_ms"] += (perf_counter()-t)*1000
                telemetry["hard_filter_candidate_count"] += count
                telemetry["vector_candidate_count"] += len(candidates)
                t = perf_counter()
                documents = tuple(self.sources.get(kind, c.document_id, c.source_version) for c in candidates)
                if any(d.content_hash != c.content_hash or not d.eligible(scope, snapshot.case_id)
                       for d, c in zip(documents, candidates)):
                    raise RetrievalError("stale retrieval projection")
                selected[kind] = rerank(candidates, documents, snapshot, signature)[:4 if kind == DocumentType.SKILL else 3]
                telemetry["reranked_count"] += len(documents)
                telemetry["rerank_latency_ms"] += (perf_counter()-t)*1000
            skills = tuple(d.source for d in selected[DocumentType.SKILL])
            experiences = tuple(StructuredExperienceReranker().capsule(d.source, signature) for d in selected[DocumentType.EXPERIENCE])
            telemetry.update(selected_skill_ids=tuple(s.skill_id for s in skills),
                selected_experience_ids=tuple(e.experience_id for e in experiences), latency_ms=(perf_counter()-start)*1000)
            self._emit(telemetry)
            return HybridSelection(skills, experiences)
        except Exception:
            telemetry.update(degradation="RETRIEVAL_FAILED", latency_ms=(perf_counter()-start)*1000)
            self._emit(telemetry)
            raise RetrievalError("optional hybrid retrieval unavailable") from None

    def guidance_provider(self):
        # Reuse the established 4/3/12000 budget and ALL-selected SkillComposer
        # safety checks. This is the existing Planner/Runtime injection seam.
        from credit_harness.memory.retrieval import VerifiedExperienceRetriever
        return InvestigationGuidanceService(self.sources.skills,
            VerifiedExperienceRetriever(self.sources.experiences), selection_provider=self)
