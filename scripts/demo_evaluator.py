"""Synthetic Step 10 demo. External progression is test-only; evaluation is not."""
import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.evaluation_fixture import EvaluationFixture, CASE
from credit_harness.persistence.store import open_engine
from credit_harness.domain.enums import ToolName as T, ScenarioId
from credit_harness.evaluation.models import EvaluationVerdict

SCENARIOS = ("s6-partially-repaired", "s6-converged", "unknown-effect", "identity-mismatch", "no-disbursement")


def run_demo(engine, scenario):
    x = EvaluationFixture(engine, scenario=ScenarioId.S3 if scenario == "no-disbursement" else ScenarioId.S6)
    try:
        if scenario == "no-disbursement":
            x.progress(no_disbursement=True)
            x.read()
        elif scenario == "identity-mismatch":
            x.progress(transaction_updates={"beneficiary_ref": "BEN-OTHER"})
            x.read()
        else:
            x.read()
            x.remediate(timeout=scenario == "unknown-effect")
            if scenario in ("s6-converged", "unknown-effect"):
                x.progress()
                x.read()
            else:
                x.read(T.MESSAGES)
        report = x.evaluate()
        closure = x.closure.close(report) if report.overall_verdict == EvaluationVerdict.PASS else None
        return dict(scenario=scenario, report=report.model_dump(mode="json"),
            closure=closure.model_dump(mode="json") if closure else None,
            case_status=x.cases.get(CASE).status.value,
            effect_status=x.effect.status.value if hasattr(x, "effect") else None,
            evidence_count=len(x.evidence.list(CASE)),
            message="REPLAY APPLIED; MESSAGE CONSUMED; BUT BUSINESS CASE NOT VERIFIED"
                if scenario == "s6-partially-repaired" else report.overall_verdict.value)
    finally:
        x.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="s6-partially-repaired")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/evaluator-demo-{uuid4().hex}.db")
    try:
        output = run_demo(engine, args.scenario)
        text = json.dumps(output, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
