import hashlib
import json

from .models import ContextBudgetUsage, ReasoningContextSnapshot


class MandatoryContextOverflow(ValueError):
    """No usable snapshot may be emitted when mandatory material cannot fit."""


def digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def case_payload(case):
    payload = case.model_dump(mode="json")
    payload["scope"]["allowed_tools"].sort()
    payload["scope"]["allowed_order_ids"].sort()
    return payload


def snapshot_digest(snapshot):
    payload = snapshot.model_dump(mode="json")
    payload.pop("snapshot_id")
    return digest(payload)


def seal(payload, limits) -> ReasoningContextSnapshot:
    usage = ContextBudgetUsage(
        serialized_chars=0, approximate_tokens=0, fact_capsules=len(payload["current_facts"]),
        hypothesis_capsules=len(payload["active_hypotheses"]) + len(payload["resolved_hypotheses_summary"]),
        gap_capsules=len(payload["open_evidence_gaps"]),
        history_items=len(payload["history_digest"].repeated_lookup_groups) + len(payload["history_digest"].state_transitions),
        limits=limits,
    )
    snapshot = ReasoningContextSnapshot(snapshot_id="0" * 64, context_budget_usage=usage, **payload)
    # Length includes the usage fields themselves; converge without tokenizer or wall clock.
    for _ in range(12):
        size = len(snapshot.model_dump_json())
        updated = usage.model_copy(update={"serialized_chars": size, "approximate_tokens": (size + 2) // 3})
        if updated == snapshot.context_budget_usage:
            return snapshot.model_copy(update={"snapshot_id": snapshot_digest(snapshot)})
        snapshot = snapshot.model_copy(update={"context_budget_usage": updated})
    raise RuntimeError("context length did not converge")


def fits(snapshot) -> bool:
    u = snapshot.context_budget_usage
    b = u.limits
    return (u.serialized_chars <= b.max_serialized_chars and u.fact_capsules <= b.max_fact_capsules
            and u.hypothesis_capsules <= b.max_hypothesis_capsules and u.gap_capsules <= b.max_gap_capsules
            and u.history_items <= b.max_history_items)


def referenced_ids(payload) -> tuple[str, ...]:
    refs = set()
    def visit(value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"evidence_refs", "decisive_evidence_refs", "supporting_evidence_refs", "contradicting_evidence_refs",
                           "supporting_ref_preview", "contradicting_ref_preview"}:
                    refs.update(item)
                elif key in {"first_ref", "latest_ref"}:
                    refs.add(item)
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(payload)
    return tuple(sorted(refs))
