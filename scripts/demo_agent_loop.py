"""Synthetic local bootstrap; runtime and fake planner never receive scenario data."""
import argparse
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo_case_evidence import LocalHTTPClient
from credit_harness.adapters.investigation_fake import InvestigationFakePlannerModel
from credit_harness.agent.runtime import InvestigationAgentRuntime
from credit_harness.api.app import create_app
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases.schema import create_harness_schema
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ScenarioId, ToolName
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine
from credit_harness.planner.service import PlannerService
from credit_harness.simulator.scenarios import build_scenario


def run_demo(engine, scenario, model=None):
    create_schema(engine)
    create_harness_schema(engine)
    admin = SimulatorAdmin(engine)
    sid = admin.seed(build_scenario(scenario))
    token = admin.grant(sid, set(ToolName))
    cases = CaseRepository(engine, "demo")
    case = cases.create(investigation_case(sid), token)
    evidence = EvidenceRepository(cases)
    with TestClient(create_app(engine)) as upstream:
        class TimedSyntheticClient(LocalHTTPClient):
            def observe(self, *args, **kwargs):
                # Trusted demo clock, same behavior for every scenario. This is
                # neither WAIT scheduling nor a runtime/model dependency.
                admin.advance(sid, 15)
                return super().observe(*args, **kwargs)
        client = TimedSyntheticClient(upstream, token)
        executor = CaseToolExecutor(cases, evidence, lambda _: client)
        runtime = InvestigationAgentRuntime(cases, evidence, ReasoningContextAssembler(),
            PlannerService(model or InvestigationFakePlannerModel()), executor)
        return runtime.run(case.case_id)


def trace(result):
    lines = []
    for turn in result.turns:
        lines.extend([f"\nTURN {turn.turn_number}", f"Snapshot: {turn.before_snapshot_id}"])
        for number, attempt in enumerate(turn.attempts, 1):
            lines.append(f"Planning attempt {number} / MODEL PROPOSED + HARNESS VALIDATION")
            for action in attempt.decision.valid_candidates:
                candidate = action.candidate
                lines.append(f"  {candidate.candidate_id}: {getattr(candidate, 'tool_name', candidate.action_type)} "
                             f"-> {candidate.target_gap_ids} ACCEPT")
            for rejected in attempt.decision.rejected_candidates:
                candidate = rejected.candidate
                lines.append(f"  {candidate.candidate_id}: {getattr(candidate, 'tool_name', candidate.action_type)} "
                             f"-> {candidate.target_gap_ids} REJECT {','.join(rejected.reason_codes)}")
            selected = attempt.decision.selected_action
            lines.append(f"Harness selected: {selected.candidate.candidate_id if selected else 'NONE'}")
            lines.append(f"Runtime revalidation: {attempt.revalidation.status}; DB CAS: {attempt.execution_cas}")
        lines.extend([f"Tool: {turn.tool_name}; Call: {turn.tool_call_id}",
                      f"Evidence added: {turn.new_evidence_refs}"])
        delta = turn.knowledge_progress
        lines.append(f"Payment identity: {delta.before.identity} -> {delta.after.identity}")
        old = {h.hypothesis_id: h.status for h in delta.before.hypotheses}
        for h in delta.after.hypotheses:
            if old.get(h.hypothesis_id) != h.status:
                lines.append(f"{h.hypothesis_id}: {old.get(h.hypothesis_id)} -> {h.status}")
        lines.append(f"Open gaps removed: {sorted(set(delta.before.open_gap_ids) - set(delta.after.open_gap_ids))}")
        lines.append(f"Open gaps added: {sorted(set(delta.after.open_gap_ids) - set(delta.before.open_gap_ids))}")
        lines.append(f"Knowledge progress: {delta.has_progress}; outcome: {turn.turn_outcome}")
    lines.extend([f"\nAGENT RUN STOPPED: {result.status}",
                  f"Turns: {result.turn_count}; dispatched: {result.tool_calls_dispatched}",
                  f"Case: {result.final_case_status} (not closed)"])
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--provider", choices=["fake", "openai"], default="fake")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    model = InvestigationFakePlannerModel()
    if args.provider == "openai":
        from credit_harness.adapters.openai_planner import OpenAIPlannerModel
        model = OpenAIPlannerModel()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/agent-demo-{uuid4().hex}.db")
    try:
        result = run_demo(engine, ScenarioId(args.scenario), model)
        print(trace(result))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
