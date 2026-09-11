from credit_harness.domain.enums import ToolName
from credit_harness.hypotheses.models import GapStatus
from .models import CallToolCandidate, RejectionCode as R


def addressed_requirements(capability, gap):
    return set(gap.required_claim_types) & (set(capability.produces_claim_types) | set(capability.contributes_requirements))


class CandidateValidator:
    def validate(self, snapshot, candidate):
        reasons = []
        gaps = {g.gap_id: g for g in snapshot.open_evidence_gaps if g.status == GapStatus.OPEN}
        if not set(candidate.target_gap_ids) <= gaps.keys():
            reasons.append(R.UNKNOWN_TARGET_GAP)
        if not isinstance(candidate, CallToolCandidate):
            return tuple(reasons)
        capability = next((t for t in snapshot.available_tools if t.tool_name == candidate.tool_name), None)
        if capability is None:
            reasons.append(R.TOOL_NOT_AVAILABLE)
        if candidate.query.internal_order_id != snapshot.internal_order_id:
            reasons.append(R.FOREIGN_ORDER)
        query = candidate.query
        if candidate.tool_name != ToolName.PROTOCOL:
            if query.protocol_version is not None or query.effective_at is not None:
                reasons.append(R.INVALID_PROTOCOL_QUERY)
        else:
            # A proposal may query only an already-observed version. No invented
            # version or arbitrary effective-time search expansion in Step 5.
            versions = {f.protocol_version for f in snapshot.current_facts if f.protocol_version}
            versions.update(g.scope.protocol_version for g in snapshot.history_digest.repeated_lookup_groups if g.scope.protocol_version)
            if query.protocol_version not in versions or query.effective_at is not None:
                reasons.append(R.INVALID_PROTOCOL_QUERY)
        if capability is not None:
            if not set(candidate.expected_claim_types) <= set(capability.produces_claim_types):
                reasons.append(R.EXPECTED_CLAIM_NOT_PRODUCED)
            if not any(addressed_requirements(capability, gaps[g]) for g in candidate.target_gap_ids if g in gaps):
                reasons.append(R.TOOL_DOES_NOT_ADDRESS_TARGET_GAP)
        return tuple(dict.fromkeys(reasons))
