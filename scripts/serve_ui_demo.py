"""Trusted local S6/S8 bootstrap, then serve ONLY the read-only UI API."""
import argparse
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from scripts.demo_case_evidence import run_demo
from credit_harness.api.ui import create_production_ui_app
from credit_harness.cases.repository import CaseRepository
from credit_harness.domain.enums import ScenarioId
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.persistence.store import open_engine, token_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--recorded-agent", action="store_true", help="Run the existing synthetic fake agent before opening the read-only server")
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/ui-demo-{uuid4().hex}.db")
    if args.recorded_agent:
        from scripts.demo_agent_loop import run_demo as run_agent_demo
        run_agent_demo(engine, ScenarioId(args.scenario))
    else:
        run_demo(engine, ScenarioId(args.scenario))
    repository = EvidenceRepository(CaseRepository(engine, "demo"))
    # Public, synthetic local-demo read-only grant. Never use for deployment.
    app = create_production_ui_app({token_hash("local-ui-demo"): repository})
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
