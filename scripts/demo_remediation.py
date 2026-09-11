"""Run synthetic investigation, then a SEPARATE no-effect remediation preview."""
import argparse
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo_agent_loop import run_demo as investigate
from credit_harness.domain.enums import ScenarioId
from credit_harness.persistence.store import open_engine
from credit_harness.cases.repository import CaseRepository
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.remediation.state import InvestigationStateReader
from credit_harness.remediation.service import RemediationPlanner
from credit_harness.adapters.remediation_fake import remediation_fake_model


def run_demo(engine, scenario, model=None):
    investigation = investigate(engine, scenario)
    cases = CaseRepository(engine, "demo")
    evidence = EvidenceRepository(cases)
    reader = InvestigationStateReader(cases, evidence)
    before = reader.read(investigation.case_id)
    service = RemediationPlanner(reader, model or remediation_fake_model())
    decision = service.plan(investigation.case_id)
    after = reader.read(investigation.case_id)
    assert before.case == after.case and before.index.evidence == after.index.evidence
    return investigation, decision


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--provider", choices=["fake", "openai"], default="fake")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    model = remediation_fake_model()
    if args.provider == "openai":
        from credit_harness.adapters.openai_remediation import OpenAIRemediationModel
        model = OpenAIRemediationModel()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/remediation-demo-{uuid4().hex}.db")
    try:
        investigation, decision = run_demo(engine, ScenarioId(args.scenario), model)
        print("INVESTIGATION STATE")
        print(investigation.turns[-1].knowledge_progress.after.model_dump_json(indent=2))
        print("MODEL PROPOSED / HARNESS VALIDATION / PREFLIGHT")
        for preview in decision.previews:
            print(preview.model_dump_json(indent=2))
        print("FINAL REMEDIATION INTENT")
        print(decision.final_intent.model_dump_json(indent=2) if decision.final_intent else "None")
        print("EXECUTION: NOT AUTHORIZED / NOT EXECUTED; Case and Evidence unchanged.")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(decision.model_dump_json(indent=2) + "\n", encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
