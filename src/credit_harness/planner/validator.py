from credit_harness.domain.enums import ToolName
from credit_harness.hypotheses.models import GapStatus, UncollectedClaimType
from .models import CallToolCandidate, EscalateCandidate, ReasonCode, RejectionCode as R


def addressed_requirements(capability, gap):
    return set(gap.required_claim_types) & (set(capability.produces_claim_types) | set(capability.contributes_requirements))


class CandidateValidator:
    def validate(self, snapshot, candidate):
        reasons = []
        gaps = {g.gap_id: g for g in snapshot.open_evidence_gaps if g.status == GapStatus.OPEN}
        if not set(candidate.target_gap_ids) <= gaps.keys():
            reasons.append(R.UNKNOWN_TARGET_GAP)
        if isinstance(candidate, EscalateCandidate):
            targets = [gaps[g] for g in candidate.target_gap_ids if g in gaps]
            requested = candidate.requested_capability
            if requested is not None and (
                not isinstance(requested, UncollectedClaimType)
                or not any(requested in g.required_claim_types for g in targets)
            ):
                reasons.append(R.ESCALATION_CAPABILITY_MISMATCH)
            # NO_AVAILABLE_TOOL is a claim about all targeted gaps, not authority
            # supplied by the model. Mixed solvable/unsolvable targets must split.
            if candidate.reason_code == ReasonCode.NO_AVAILABLE_TOOL and any(
                addressed_requirements(tool, g) for g in targets for tool in snapshot.available_tools
            ):
                reasons.append(R.ESCALATION_REASON_INCONSISTENT)
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
