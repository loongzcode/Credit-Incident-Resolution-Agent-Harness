"""Offline proposal heuristic using only ModelInputBundle, never a scenario script."""
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.models import PriorityClass as P, UncollectedClaimType
from credit_harness.planner.models import (
    ModelInputBundle, PlannerDraft, PlannerModelMetadata, CallToolCandidate, WaitCandidate,
    EscalateCandidate, ProposalQuery, ReasonCode,
)
from credit_harness.planner.validator import addressed_requirements


class InvestigationFakePlannerModel:
    """Test/demo model, not a claim of LLM reasoning quality or optimal planning."""
    metadata = PlannerModelMetadata(model_provider="fake", model_name="context-driven-investigation-v1")

    def plan(self, model_input: ModelInputBundle) -> PlannerDraft:
        if type(model_input) is not ModelInputBundle:
            raise TypeError("only model input bundle accepted")
        control, derived, external = (model_input.trusted_control, model_input.deterministic_derived,
                                      model_input.untrusted_external_data)
        gaps, tools = derived.open_evidence_gaps, control.available_tools
        safety = tuple(g for g in gaps if g.priority == P.SAFETY_CRITICAL
                       and any(addressed_requirements(t, g) for t in tools))
        targets = safety or gaps
        if not targets:
            # Valid schema still requires targets. This unsupported empty state
            # fails closed at the Planner rather than fabricating a gap/tool.
            from credit_harness.planner.models import PlannerProtocolError
            raise PlannerProtocolError("fake planner has no open investigation target")
        fallback_ids = tuple(g.gap_id for g in targets)
        history = external.history_digest
        blocked = (not control.budget.investigation_allowed or control.budget.remaining_tool_calls == 0
                   or not history.lookup_history_complete)
        if blocked:
            fallback = EscalateCandidate(candidate_id="fallback", target_gap_ids=fallback_ids,
                reason_code=ReasonCode.BUDGET_EXHAUSTED if control.budget.remaining_tool_calls == 0 else ReasonCode.POLICY_BLOCKED,
                reason_summary="预算、调查状态或历史完整性限制阻止继续查询。")
            return self._draft(model_input, (fallback,))
        current = {(f.source.tool, f.claim_type, f.protocol_version) for f in external.current_facts}
        versions = sorted({f.protocol_version for f in external.current_facts if f.protocol_version})
        choices = []
        priorities = {P.SAFETY_CRITICAL: 3, P.DISCRIMINATING: 2, P.SUPPORTING: 1}
        repeated_block = False
        for tool in tools:
            if tool.tool_name == T.CALLBACK_RAW:
                continue  # fake prefers the minimized gateway DTO
            for version in versions if tool.tool_name == T.PROTOCOL else (None,):
                addressed = tuple(g for g in targets if addressed_requirements(tool, g))
                if not addressed:
                    continue
                query = ProposalQuery(internal_order_id=control.internal_order_id, protocol_version=version)
                groups = [g for g in history.repeated_lookup_groups
                          if g.tool == tool.tool_name and g.scope.model_dump() == query.model_dump()]
                if any(g.latest_consecutive_count >= 3 for g in groups):
                    repeated_block = True
                    continue
                # Re-query missing observations, but don't repeatedly query already
                # observed fields hoping an unsupported deployment fact will appear.
                novel = {c for g in addressed for c in addressed_requirements(tool, g)
                         if not any(t == tool.tool_name and claim == c
                                    and (version is None or v == version) for t, claim, v in current)}
                if not novel:
                    continue
                # Association metadata is already present when this source has a
                # current fact. Let another source establish the missing link.
                novel = {c for c in novel if not isinstance(c, UncollectedClaimType)
                         or not any(t == tool.tool_name for t, _, _ in current)}
                if not novel:
                    continue
                candidate = CallToolCandidate(candidate_id="investigate", tool_name=tool.tool_name,
                    target_gap_ids=tuple(g.gap_id for g in addressed), query=query,
                    expected_claim_types=tool.produces_claim_types,
                    reason_summary="查询当前缺口所需且尚未观察到的字段；关联与结论由 Harness 确定性验证。")
                choices.append(((-max(priorities[g.priority] for g in addressed), -len(novel),
                                 -len({h for g in addressed for h in g.related_hypotheses}),
                                 tool.tool_name.value, version or ""), candidate))
        if not choices:
            if repeated_block:
                candidate = WaitCandidate(candidate_id="wait", target_gap_ids=fallback_ids,
                    reason_code=ReasonCode.REPEATED_SOURCE_FAILURE, suggested_wait_seconds=60,
                    reason_summary="重复查询没有提供足够事实，等待外部来源变化；资金仍为 UNKNOWN。")
            else:
                unsolvable = tuple(g for g in targets if not any(addressed_requirements(t, g) for t in tools))
                chosen = unsolvable or targets
                requested = next((c for g in chosen for c in g.required_claim_types
                                  if isinstance(c, UncollectedClaimType)), None)
                candidate = EscalateCandidate(candidate_id="escalate", target_gap_ids=tuple(g.gap_id for g in chosen),
                    reason_code=ReasonCode.NO_AVAILABLE_TOOL if unsolvable else ReasonCode.INSUFFICIENT_EVIDENCE,
                    requested_capability=requested, reason_summary="现有可观察字段不足以填补剩余缺口，需要外层调查。")
            return self._draft(model_input, (candidate,))
        candidate = sorted(choices, key=lambda item: item[0])[0][1]
        # Expose a deliberately irrelevant proposal in the demo; policy must reject
        # it regardless of the model's explanation. Runtime never creates it.
        irrelevant = CallToolCandidate(candidate_id="irrelevant", target_gap_ids=candidate.target_gap_ids,
            tool_name=T.ACCOUNTING, query=ProposalQuery(internal_order_id=control.internal_order_id),
            expected_claim_types=(C.ACCOUNTING_ENTRY_PRESENT,), reason_summary="也许查账务就能解释。")
        fallback = EscalateCandidate(candidate_id="fallback", target_gap_ids=candidate.target_gap_ids,
            reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, reason_summary="无法安全调查时交由外层处理。")
        return self._draft(model_input, (candidate, irrelevant, fallback))

    @staticmethod
    def _draft(bundle, candidates):
        return PlannerDraft(snapshot_id=bundle.snapshot_id, candidates=candidates,
            observation_summary="仅依据本次分区 Snapshot 投影。", uncertainty_summary="缺少观测不代表业务失败。")
