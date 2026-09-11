from credit_harness.context.models import ToolRisk
from .models import CallToolCandidate, RejectionCode as R
from .validator import CandidateValidator

REPEATED_FAILURE_THRESHOLD = 3


def repeated_count(snapshot, candidate):
    return max((g.latest_consecutive_count for g in snapshot.history_digest.repeated_lookup_groups
                if g.tool == candidate.tool_name and g.scope.model_dump() == candidate.query.model_dump()
                and g.status.value != "OK"), default=0)


class HardPolicyFilter:
    def filter(self, snapshot, candidate):
        # Defense in depth: directly invoking the filter cannot bypass validation.
        reasons = list(CandidateValidator().validate(snapshot, candidate))
        if isinstance(candidate, CallToolCandidate):
            if not snapshot.budget.investigation_allowed:
                reasons.append(R.INVESTIGATION_NOT_ALLOWED)
            if snapshot.budget.remaining_tool_calls <= 0 or snapshot.budget.used_tool_calls >= snapshot.budget.max_tool_calls:
                reasons.append(R.BUDGET_EXHAUSTED)
            capability = next((t for t in snapshot.available_tools if t.tool_name == candidate.tool_name), None)
            if capability is not None and capability.risk_class != ToolRisk.READ_ONLY:
                reasons.append(R.NON_READ_ONLY_TOOL)
            if not snapshot.history_digest.lookup_history_complete:
                # Missing compacted history cannot be interpreted as zero failures.
                reasons.append(R.INCOMPLETE_LOOKUP_HISTORY)
            if repeated_count(snapshot, candidate) >= REPEATED_FAILURE_THRESHOLD:
                reasons.append(R.REPEATED_NO_NEW_INFORMATION)
        return tuple(dict.fromkeys(reasons))
