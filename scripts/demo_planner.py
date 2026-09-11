"""Manual simulator investigation, followed by one proposal-only planner call."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo_case_evidence import run_demo
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ScenarioId, ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.persistence.store import open_engine
from credit_harness.planner.models import (
    PlannerDraft, CallToolCandidate, EscalateCandidate, ProposalQuery, ReasonCode, PlannerUnavailable,
)
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.service import PlannerService

STAGES = {
    "S6-A": (ScenarioId.S6, (T.TRACE,)),
    "S6-B": (ScenarioId.S6, (T.TRACE, T.PAYMENT)),
    "S6-C": (ScenarioId.S6, (T.TRACE, T.PAYMENT, T.CALLBACK)),
    "S6-D": (ScenarioId.S6, None),
    "S8-initial": (ScenarioId.S8, (T.FUND,)),
    "S8-repeated": (ScenarioId.S8, None),
}


def scripted_draft(bundle):
    """Illustrative fake proposals; deliberately includes an irrelevant tool."""
    gaps = {g.gap_id.rsplit(":", 1)[-1]: g for g in bundle.deterministic_derived.open_evidence_gaps}
    choices = (("PAYMENT_FINALITY", T.PAYMENT, C.PAYMENT_FINALITY),
               ("FUND_PROTOCOL_APPLICABILITY", T.GUARANTEE, C.GUARANTEE_STATUS),
               ("CALLBACK_CONSUMPTION", T.MESSAGES, C.MESSAGE_CONSUME_STATUS),
               ("ASSET_CONVERGENCE", T.GUARANTEE, C.GUARANTEE_STATUS))
    name, tool, claim = next(item for item in choices if item[0] in gaps)
    target = (gaps[name].gap_id,)
    query = ProposalQuery(internal_order_id=bundle.trusted_control.internal_order_id)
    return PlannerDraft(snapshot_id=bundle.snapshot_id, candidates=(
        CallToolCandidate(candidate_id="candidate-1", target_gap_ids=target, tool_name=tool, query=query,
                          expected_claim_types=(claim,), reason_summary="查询当前缺口对应的直接业务字段或关联元数据。"),
        CallToolCandidate(candidate_id="candidate-2", target_gap_ids=target, tool_name=T.ACCOUNTING, query=query,
                          expected_claim_types=(C.ACCOUNTING_ENTRY_PRESENT,), reason_summary="查一下账务。"),
        EscalateCandidate(candidate_id="candidate-3", target_gap_ids=target,
                          reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, reason_summary="当前证据不足时交由外层处理。"),
    ), observation_summary="仅依据当前 Snapshot 中的事实。", uncertainty_summary="未知资金事实保持 UNKNOWN。")


def build_stage(engine, stage):
    scenario, tools = STAGES[stage]
    view = run_demo(engine, scenario, manual_steps=[(t, None) for t in tools] if tools is not None else None)
    evidence = tuple(e for values in view.evidence_by_claim_type.values() for e in values)
    return ReasoningContextAssembler().build(view.case, evidence)


def trace(snapshot, decision):
    proposed = [p.candidate for p in decision.valid_candidates] + [r.candidate for r in decision.rejected_candidates]
    lines = [f"SNAPSHOT {snapshot.snapshot_id}", f"Identity: {snapshot.financial_identity.result.value}", "MODEL PROPOSED"]
    for c in sorted(proposed, key=lambda c: c.candidate_id):
        lines.append(f"{c.candidate_id}: {c.action_type.value} {getattr(c, 'tool_name', '')} -> {','.join(c.target_gap_ids)}")
    lines.append("HARNESS")
    lines.extend(f"{p.candidate.candidate_id}: VALID" for p in decision.valid_candidates)
    lines.extend(f"{r.candidate.candidate_id}: REJECT {','.join(r.reason_codes)}" for r in decision.rejected_candidates)
    lines.append("RANKING")
    lines.extend(d.model_dump_json() for d in decision.ranking_details)
    selected = decision.selected_action.candidate if decision.selected_action else None
    lines.extend([f"SELECTED: {getattr(selected, 'tool_name', getattr(selected, 'action_type', 'NONE'))}",
                  decision.selection_reason, "STOP: proposal was not executed."])
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["fake", "openai"], default="fake")
    parser.add_argument("--stage", choices=[*STAGES, "all"], default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    model = FakePlannerModel(scripted_draft)
    if args.provider == "openai":
        from credit_harness.adapters.openai_planner import OpenAIPlannerModel
        model = OpenAIPlannerModel()
    results = {}
    Path(".local").mkdir(exist_ok=True)
    for stage in STAGES if args.stage == "all" else [args.stage]:
        engine = open_engine(f"sqlite:///.local/planner-demo-{uuid4().hex}.db")
        try:
            snapshot = build_stage(engine, stage)  # Completed before any model call.
            service = PlannerService(model)
            decision = service.plan(snapshot)
            print(f"\n{stage}\n{trace(snapshot, decision)}")
            results[stage] = {"snapshot": snapshot.model_dump(mode="json"),
                              "decision": decision.model_dump(mode="json"),
                              "audit": service.audit.records[-1].model_dump(mode="json")}
        except PlannerUnavailable:
            print(f"{stage}: PlannerUnavailable; no fallback action executed.")
        finally:
            engine.dispose()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
