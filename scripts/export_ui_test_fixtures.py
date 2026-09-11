"""Regenerate UI TEST fixtures through real routes. Never consumed at runtime."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from scripts.demo_case_evidence import run_demo
from credit_harness.api.ui import create_ui_app
from credit_harness.cases.repository import CaseRepository
from credit_harness.domain.enums import ScenarioId
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.persistence.store import open_engine, token_hash

if __name__ == '__main__':
    target = Path(__file__).resolve().parents[1] / 'frontend/src/test/fixtures'
    target.mkdir(parents=True, exist_ok=True)
    for scenario in (ScenarioId.S6, ScenarioId.S8):
        with TemporaryDirectory() as temp:
            engine = open_engine(f'sqlite:///{Path(temp).as_posix()}/demo.db')
            run_demo(engine, scenario)
            repository = EvidenceRepository(CaseRepository(engine, 'demo'))
            with TestClient(create_ui_app({token_hash('test'): repository})) as client:
                output = {}
                for key, suffix in [('case', ''), ('evidence', '/evidence'), ('graph', '/hypotheses'), ('context', '/reasoning-context')]:
                    response = client.get('/ui/cases/CASE-JD202609100001' + suffix, headers={'Authorization': 'Bearer test'})
                    response.raise_for_status()
                    output[key] = response.json()
                (target / f'{scenario.value.lower()}.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            engine.dispose()
