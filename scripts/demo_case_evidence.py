"""Trusted local bootstrap + manual HTTP investigation; no Agent or reasoning.

Both FastAPI applications run through their real routes using an in-process HTTP
test transport, so this demo needs no open ports or external credentials.
"""
import argparse
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from credit_harness.api.app import create_app
from credit_harness.api.harness import HarnessBinding, create_harness_app
from credit_harness.cases.evidence_view import CaseEvidenceService, CaseEvidenceView
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases.schema import create_harness_schema
from credit_harness.cases.service import CaseService
from credit_harness.domain.enums import ScenarioId, ToolName
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine, token_hash
from credit_harness.simulator.scenarios import build_scenario
from credit_harness.tools.contracts import DISPATCH_CORRELATION_HEADER, Observation, ToolQuery


class LocalHTTPClient:
    def __init__(self, http, token):
        self._http, self._token = http, token

    def observe(self, tool, query, *, dispatch_correlation_id):
        response = self._http.post(f"/tools/{tool.value}", json=query.model_dump(mode="json"),
                                    headers={"Authorization": f"Bearer {self._token}",
                                             DISPATCH_CORRELATION_HEADER: dispatch_correlation_id})
        response.raise_for_status()
        return Observation.model_validate(response.json())


def run_demo(engine, scenario: ScenarioId = ScenarioId.S6, *, manual_steps=None) -> CaseEvidenceView:
    create_schema(engine)
    create_harness_schema(engine)
    # Only this trusted provisioning section knows the scenario; runtime receives a
    # simulation ID and scoped credential, never an admin object or scenario label.
    admin = SimulatorAdmin(engine)
    sid = admin.seed(build_scenario(scenario))
    token = admin.grant(sid, set(ToolName))
    cases = CaseRepository(engine, "demo")
    case = CaseService(cases).create(investigation_case(sid), tool_credential=token)
    repository = EvidenceRepository(cases)
    with TestClient(create_app(engine)) as upstream:
        tool_client = LocalHTTPClient(upstream, token)
        executor = CaseToolExecutor(cases, repository, lambda _: tool_client)
        binding = HarnessBinding(executor, CaseEvidenceService(repository))
        harness_secret = secrets.token_urlsafe(32)
        with TestClient(create_harness_app({token_hash(harness_secret): binding})) as harness:
            headers = {"Authorization": f"Bearer {harness_secret}"}
            steps = [(tool, None) for tool in (
                ToolName.TRACE, ToolName.FUND, ToolName.PAYMENT, ToolName.CALLBACK, ToolName.MESSAGES,
            )] + [(ToolName.PROTOCOL, "2.3"), (ToolName.PROTOCOL, "2.2")]
            if scenario == ScenarioId.S8:
                steps = [(tool, None) for _ in range(3) for tool in (ToolName.FUND, ToolName.PAYMENT)]
            if manual_steps is not None:
                steps = manual_steps
            for tool, version in steps:
                response = harness.post(f"/cases/{case.case_id}/tools/{tool.value}", headers=headers,
                                        json=ToolQuery(internal_order_id=case.internal_order_id,
                                                       protocol_version=version).model_dump(mode="json"))
                response.raise_for_status()
                if scenario == ScenarioId.S8:
                    admin.advance(sid, 15)  # trusted clock only; never a Harness endpoint
            response = harness.get(f"/cases/{case.case_id}/evidence", headers=headers)
            response.raise_for_status()
            return CaseEvidenceView.model_validate(response.json())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    local = Path(".local")
    local.mkdir(exist_ok=True)
    database = local / f"case-demo-{uuid4().hex}.db"
    engine = open_engine(f"sqlite:///{database.as_posix()}")
    try:
        view = run_demo(engine, ScenarioId(args.scenario))
        payload = view.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
            print(f"case={view.case.case_id} calls={view.case.budget.used_tool_calls} evidence={view.evidence_count}")
            print(f"JSON: {args.output.resolve()}")
            print(f"Provenance database: {database.resolve()}")
        else:
            print(payload)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
