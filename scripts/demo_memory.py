"""Offline synthetic organizational memory demo; no claims about live LLM quality."""
import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support.evaluation_fixture import EvaluationFixture
from credit_harness.persistence.store import open_engine
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.models import PriorityClass
from credit_harness.memory.models import StrategyGoal
from credit_harness.memory.tables import create_memory_schema
from credit_harness.memory.repository import SQLExperienceRepository
from credit_harness.memory.experience import VerifiedExperiencePublisher
from credit_harness.memory.skills import SQLSkillRepository, general_investigation_skill
from credit_harness.memory.retrieval import VerifiedExperienceRetriever
from credit_harness.memory.guidance import InvestigationGuidanceService
from credit_harness.memory.aggregation import ExperiencePatternAggregator
from credit_harness.planner.models import PlannerDraft, CallToolCandidate, EscalateCandidate, ProposalQuery, ReasonCode
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.service import PlannerService
from credit_harness.agent.runtime import InvestigationAgentRuntime
from credit_harness.agent.models import AgentRunConfig


def illustrative_guided_draft(bundle):
    """Fake makes guidance consumption inspectable; real model quality is unmeasured."""
    gaps = bundle.deterministic_derived.open_evidence_gaps
    candidates = []
    if bundle.organizational_guidance:
        for skill in bundle.organizational_guidance.active_skills:
            for strategy in skill.evidence_strategy:
                for gap in gaps:
                    claims = set(strategy.recommended_claim_types) & set(gap.required_claim_types)
                    for tool in bundle.trusted_control.available_tools:
                        supported = claims & set(tool.produces_claim_types)
                        if supported and strategy.priority == PriorityClass.SAFETY_CRITICAL:
                            candidates.append(CallToolCandidate(candidate_id="guided-current-read", target_gap_ids=(gap.gap_id,),
                                tool_name=tool.tool_name, query=ProposalQuery(internal_order_id=bundle.trusted_control.internal_order_id),
                                expected_claim_types=tuple(sorted(supported)), reason_summary="Active skill suggests obtaining current safety evidence."))
                            break
                    if candidates:
                        break
                if candidates:
                    break
            if candidates:
                break
    safety = next((g for g in gaps if g.priority == PriorityClass.SAFETY_CRITICAL), gaps[0])
    candidates.append(EscalateCandidate(candidate_id="safe-fallback", target_gap_ids=(safety.gap_id,),
        reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, reason_summary="Current evidence remains required."))
    return PlannerDraft(snapshot_id=bundle.snapshot_id, candidates=tuple(candidates),
        observation_summary="Historical guidance is separate from current facts.", uncertainty_summary="Never infer settlement from a historical pattern.")


def run_demo(engine):
    instances = []
    try:
        experiences = []
        for name in ("CASE-MEMORY-A", "CASE-MEMORY-B"):
            x = EvaluationFixture(engine, case_id=name)
            instances.append(x)
            create_memory_schema(engine)
            x.read(T.PAYMENT)  # observed early yield, not a prescribed runtime DAG
            x.read(); x.remediate(); x.read(T.MESSAGES, T.ASSET, T.GUARANTEE, T.ACCOUNTING)
            x.progress(); x.read(); x.closure.close(x.evaluate())
            repo = SQLExperienceRepository(x.cases, clock=lambda: x.clock.now)
            experiences.append(VerifiedExperiencePublisher(repo).publish(name))
        c = EvaluationFixture(engine, case_id="CASE-MEMORY-C")
        instances.append(c)
        c.advance(200)
        c.progress(no_disbursement=True)  # Oracle only in synthetic fixture
        c.read(T.TRACE)
        memory = SQLExperienceRepository(c.cases, clock=lambda: c.clock.now)
        skills = SQLSkillRepository(engine, c.case.tenant_id, clock=lambda: c.clock.now)
        skill = general_investigation_skill(); skills.add(skill); skills.activate(skill.skill_id, skill.version)
        guidance = InvestigationGuidanceService(skills, VerifiedExperienceRetriever(memory))
        assembler = ReasoningContextAssembler()
        snapshot = assembler.build(c.cases.get(c.case.case_id), c.evidence.list(c.case.case_id))
        cold = PlannerService(FakePlannerModel(illustrative_guided_draft)).plan(snapshot)
        planner = PlannerService(FakePlannerModel(illustrative_guided_draft), guidance_provider=guidance)
        proposed = planner.plan(snapshot)
        runtime = InvestigationAgentRuntime(c.cases, c.evidence, assembler, planner, c.executor,
                                            config=AgentRunConfig(max_turns=1))
        runtime.run(c.case.case_id)  # existing Step 6 revalidation + CAS, current C Tool only
        payment = tuple(e for e in c.evidence.list(c.case.case_id) if e.claim_type == C.PAYMENT_FINALITY)
        c.read()  # explicit demo investigation of remaining current sources
        graph = HypothesisEngine().evaluate(c.cases.get(c.case.case_id), c.evidence.list(c.case.case_id))
        report = c.evaluator.evaluate(c.case.case_id)
        aggregator = ExperiencePatternAggregator(memory)
        return dict(mode="OFFLINE_FAKE; no measured model-quality or efficiency claim",
            notice="HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE", experience_count=len(experiences),
            source_closures=[dict(case_id=e.source_case_id, closure_id=e.closure_id, report_id=e.report_id) for e in experiences],
            guidance=guidance.build(snapshot).model_dump(mode="json"),
            cold_selected=cold.selected_action.candidate.action_type.value,
            guided_selected=proposed.selected_action.candidate.model_dump(mode="json"),
            planner_audit=planner.audit.records[0].model_dump(mode="json"),
            current_payment=[dict(case_id=e.case_id, evidence_id=e.evidence_id, observation_id=e.observation_id, value=e.value) for e in payment],
            current_hypotheses={h.hypothesis_id.value: h.status.value for h in graph.hypotheses},
            current_evaluation=dict(verdict=report.overall_verdict.value, outcome_path=report.outcome_path.value),
            current_case_status=c.cases.get(c.case.case_id).status.value,
            statistics=aggregator.summarize().model_dump(mode="json"),
            skill_improvement_proposal=aggregator.propose(skill, c.clock.now).model_dump(mode="json"))
    finally:
        for x in reversed(instances):
            x.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/memory-demo-{uuid4().hex}.db")
    try:
        output = json.dumps(run_demo(engine), ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
