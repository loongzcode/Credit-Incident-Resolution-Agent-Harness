"""Explicit synthetic ticks; no scheduler, live provider or real financial data."""
import argparse
import json
import os
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import create_engine

from credit_harness.benchmark.fixture import EvaluationFixture
from credit_harness.agent.runtime import InvestigationAgentRuntime
from credit_harness.agent.models import AgentRunConfig
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ScenarioId, ToolName as Tool
from credit_harness.evidence.models import ClaimType
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.service import PlannerService
from credit_harness.planner.models import (PlannerDraft, CallToolCandidate, ProposalQuery,
    WaitCandidate, EscalateCandidate, ReasonCode)
from credit_harness.orchestration.models import (OrchestrationBudget, ResolutionSignal,
    SignalType, WorkReason, WorkStatus, WorkType)
from credit_harness.orchestration.repository import WorkRepository
from credit_harness.orchestration.service import DurableCaseOrchestrator


def payment_then_wait(bundle):
    gap = next(g.gap_id for g in bundle.deterministic_derived.open_evidence_gaps
               if g.gap_id.endswith(":PAYMENT_FINALITY"))
    recent = [g for g in bundle.untrusted_external_data.history_digest.repeated_lookup_groups
              if g.tool == Tool.PAYMENT and g.latest_consecutive_count > 0]
    candidate = (WaitCandidate(candidate_id="wait", target_gap_ids=(gap,),
        reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, suggested_wait_seconds=60,
        reason_summary="等待新的来源观测，UNKNOWN 不等于失败。") if recent else
        CallToolCandidate(candidate_id="payment", tool_name=Tool.PAYMENT, target_gap_ids=(gap,),
            query=ProposalQuery(internal_order_id=bundle.trusted_control.internal_order_id),
            expected_claim_types=(ClaimType.PAYMENT_FINALITY,), reason_summary="查询支付终态。"))
    return PlannerDraft(snapshot_id=bundle.snapshot_id, candidates=(candidate,),
        observation_summary="只引用 Snapshot 观测", uncertainty_summary="Payment remains UNKNOWN")


def escalate_source(bundle):
    draft = payment_then_wait(bundle)
    return draft.model_copy(update=dict(candidates=(EscalateCandidate(candidate_id="escalate",
        target_gap_ids=draft.candidates[0].target_gap_ids, reason_code=ReasonCode.REPEATED_SOURCE_FAILURE,
        reason_summary="等待来源恢复信号后重新获取 Observation。"),)))


def setup(engine, scenario, model):
    x = EvaluationFixture(engine, scenario=scenario, max_tool_calls=24)
    x.work = WorkRepository(x.cases, clock=lambda: x.clock.now)
    x.work.configure(x.case.case_id, OrchestrationBudget(max_resume_cycles=3, max_verification_cycles=5))
    x.agent = InvestigationAgentRuntime(x.cases, x.evidence, ReasoningContextAssembler(),
        PlannerService(FakePlannerModel(model)), x.executor, config=AgentRunConfig(max_turns=3))
    x.worker = DurableCaseOrchestrator(x.work, x.evidence, x.executor, x.agent, x.evaluator, x.closure)
    return x


def run_summary(run):
    return dict(run_id=run.run_id, lineage=run.lineage.model_dump(mode="json") if run.lineage else None,
        initial_snapshot_id=run.initial_snapshot_id, final_snapshot_id=run.final_snapshot_id,
        status=run.status.value, tool_calls_dispatched=run.tool_calls_dispatched)


def summary(x):
    evidence = x.evidence.list(x.case.case_id)
    return dict(case=x.cases.get(x.case.case_id).model_dump(mode="json"),
        orchestration=x.work.state(x.case.case_id).model_dump(mode="json"),
        evidence_count=len(evidence), payment_finality=[e.value for e in evidence
            if e.claim_type == ClaimType.PAYMENT_FINALITY],
        payment_lookup_results=[e.value for e in evidence
            if e.claim_type == ClaimType.SOURCE_LOOKUP_STATUS and e.tool == Tool.PAYMENT],
        work_items=[w.model_dump(mode="json") for w in x.work.list(x.case.case_id)],
        audit=[a.model_dump(mode="json") for a in x.work.audit(x.case.case_id)])


def wait_resume(x):
    first = x.agent.run(x.case.case_id)
    runs = [run_summary(first)]; checks = []
    item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == first.run_id)
    for cycle in range(3):
        before = x.evidence.list(x.case.case_id)
        x.advance(max(0, ceil((item.not_before-x.clock.now).total_seconds())))
        due = x.work.poll_due_work()
        checks.append(dict(time_passing_created_evidence=x.evidence.list(x.case.case_id) != before,
            work_ready=item.work_item_id in due, at=x.clock.now.isoformat()))
        run = x.worker.process(x.work.claim(item.work_item_id, f"worker-{cycle+2}"))
        runs.append(run_summary(run))
        item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == run.run_id)
    return dict(scenario="wait-resume", runs=runs, time_checks=checks, **summary(x))


def effect_verification(x):
    x.read()  # Explicit setup investigation through actual read Tools.
    effect = x.remediate()  # Synthetic operator approval + existing Step 8 execution only in fixture.
    work = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED)
    x.advance(1)
    first = x.worker.process(x.work.claim(work.work_item_id, "verification-worker-1"))
    x.progress()  # External synthetic systems converge; this does not write Evidence.
    x.advance(31)
    due = x.work.poll_due_work()
    item = next(i for i in due if x.work.get(i).work_type == WorkType.VERIFICATION_REQUIRED)
    final = x.worker.process(x.work.claim(item, "verification-worker-2"))
    closure = x.repository.closure(x.case.case_id)
    return dict(scenario="effect-verification", effect_status=effect.status.value,
        first_evaluation=first.overall_verdict.value, final_evaluation=final.overall_verdict.value,
        closure=closure.model_dump(mode="json"), **summary(x))


def escalation_resolution(x):
    initial = x.agent.run(x.case.case_id)
    work = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == initial.run_id)
    x.advance(60)
    elapsed_alone_resumable = x.work.claim(work.work_item_id, "before-signal") is not None
    before = x.evidence.list(x.case.case_id)
    signal = ResolutionSignal(signal_id="SIG-SYNTHETIC-RECOVERY", tenant_id=x.cases.tenant_id,
        case_id=x.case.case_id, work_item_id=work.work_item_id, signal_type=SignalType.SOURCE_RECOVERED,
        subject=x.case.internal_order_id, created_at=x.clock.now, source_actor_ref="SYNTHETIC-OPERATOR")
    x.work.submit_signal(signal)
    signal_created_evidence = x.evidence.list(x.case.case_id) != before
    x.agent.planner = PlannerService(FakePlannerModel(payment_then_wait))
    resumed = x.worker.process(x.work.claim(work.work_item_id, "after-signal"))
    return dict(scenario="escalation-resolution", elapsed_alone_resumable=elapsed_alone_resumable,
        signal_created_evidence=signal_created_evidence, signal=signal.model_dump(mode="json"),
        runs=[run_summary(initial), run_summary(resumed)], **summary(x))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("wait-resume", "effect-verification", "escalation-resolution"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # Ephemeral fake key in a local-only demo; never logged or exported.
    os.environ.setdefault("CAPABILITY_SIGNING_SECRET", "synthetic-orchestration-demo-only")
    with TemporaryDirectory(prefix="orchestration-") as temporary:
        engine = create_engine("sqlite:///" + str(Path(temporary)/"demo.db"))
        x = setup(engine, ScenarioId.S6 if args.scenario == "effect-verification" else ScenarioId.S8,
                  escalate_source if args.scenario == "escalation-resolution" else payment_then_wait)
        try:
            result = {"wait-resume": wait_resume, "effect-verification": effect_verification,
                "escalation-resolution": escalation_resolution}[args.scenario](x)
        finally:
            x.close(); engine.dispose()
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered+"\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
