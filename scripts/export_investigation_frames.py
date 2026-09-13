"""Export safe S6/S8 frames from actual persisted synthetic Agent runs."""
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from scripts.demo_agent_loop import run_demo
from credit_harness.api.ui import create_production_ui_app
from credit_harness.cases.repository import CaseRepository
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.persistence.store import open_engine, token_hash
from credit_harness.domain.enums import ScenarioId


def main():
    Path('.local').mkdir(exist_ok=True)
    target = Path('docs/examples')
    target.mkdir(exist_ok=True)
    for scenario in (ScenarioId.S6, ScenarioId.S8):
        engine = open_engine(f'sqlite:///.local/frame-export-{uuid4().hex}.db')
        try:
            run = run_demo(engine, scenario)
            repository = EvidenceRepository(CaseRepository(engine, 'demo'))
            with TestClient(create_production_ui_app({token_hash('synthetic-export'): repository})) as client:
                response = client.get(f'/ui/cases/{run.case_id}/frame', headers={'Authorization':'Bearer synthetic-export'})
                response.raise_for_status()
                frame = response.json()
                (target / f'step16-{scenario.value.lower()}-frame.json').write_text(
                    json.dumps(frame, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                print(scenario.value, 'status=', frame['case_summary']['status'],
                    'identity=', frame['financial_identity']['result'],
                    'evidence=', frame['evidence']['total'], 'planner=', frame['planner_trace']['total'],
                    'tools=', frame['tool_trace']['total'])
        finally:
            engine.dispose()


if __name__ == '__main__':
    main()
