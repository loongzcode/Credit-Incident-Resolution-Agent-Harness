"""Reproducible offline human-governance roundtrip; no financial effects or SSO secrets."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine
from credit_harness.benchmark.fixture import EvaluationFixture
from credit_harness.registry.fixtures import synthetic_registry, synthetic_route
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.registry.models import RegistryError
from credit_harness.registry_admin.identity import EnterpriseIdentity, ApplicationAccess, Role
from credit_harness.registry_admin.models import CreateDraft, EditDraft, RollbackRequest, AdminError
from credit_harness.registry_admin.service import RegistryAdministration, masked
from credit_harness.registry_admin.impact import analyze


def run(engine):
    x = EvaluationFixture(engine, ready=False)
    try:
        access = ApplicationAccess({'editor': (Role.REGISTRY_EDITOR, Role.REGISTRY_APPROVER),
            'approver': (Role.REGISTRY_APPROVER,), 'activator': (Role.REGISTRY_ACTIVATOR,), 'auditor': (Role.REGISTRY_AUDITOR,)})
        actors = {name: EnterpriseIdentity(user_id=name, display_name='Synthetic ' + name, groups=(name,),
            authenticated_at=datetime.now(timezone.utc)) for name in ('editor','approver','activator','auditor')}
        s = RegistryAdministration(engine, x.case.tenant_id, access)
        original = s.admin.register(synthetic_registry(), actor='synthetic-bootstrap')
        s.admin.activate(original, expected_version=None, actor='synthetic-bootstrap')
        s.admin.bind_case_route(x.cases, synthetic_route(x.case), actor='synthetic-bootstrap')
        catalog = RegistryBackedCatalog(CapabilityResolver(s.repo))
        snapshot = catalog.project(x.case)[1]
        cr = s.create(actors['editor'], CreateDraft(title='更新内部账务系统资料', reason='Synthetic governance demonstration',
            base_registry_version=original), 'demo-create')
        definition = masked(s.repo.current()[1])
        definition['systems'][0]['display_name'] = '我司内部账务系统（synthetic 已复核）'
        cr = s.edit(actors['editor'], cr.change_request_id, EditDraft(expected_revision=cr.revision,
            title=cr.title, reason=cr.reason, definition=definition), 'demo-edit')
        impact = analyze(s, actors['editor'], cr.change_request_id)
        diff = s.diff(actors['editor'], original, cr.proposed_registry_version)
        cr = s.transition(actors['editor'], cr.change_request_id, 'submit', cr.revision, 'demo-submit')
        try:
            s.transition(actors['editor'], cr.change_request_id, 'approve', cr.revision, 'demo-self-approve')
            raise AssertionError('self approval unexpectedly allowed')
        except AdminError as e:
            denied = e.code
        cr = s.transition(actors['approver'], cr.change_request_id, 'approve', cr.revision, 'demo-approve')
        cr = s.transition(actors['activator'], cr.change_request_id, 'activate', cr.revision, 'demo-activate')
        try:
            catalog.revalidate_snapshot(x.case, snapshot)
            raise AssertionError('old snapshot unexpectedly current')
        except RegistryError as e:
            stale = e.code.value
        rollback = s.create(actors['editor'], RollbackRequest(title='恢复历史配置', reason='Synthetic audited rollback',
            base_registry_version=cr.proposed_registry_version, target_version=original), 'demo-rollback', rollback=True)
        for action, actor in (('submit','editor'), ('approve','approver'), ('activate','activator')):
            rollback = s.transition(actors[actor], rollback.change_request_id, action, rollback.revision, 'demo-rollback-' + action)
        return dict(synthetic=True, case_id=x.case.case_id, self_approval=denied, activated_change_request=cr.model_dump(mode='json'),
            version_diff=diff.model_dump(mode='json'), impact=impact, old_snapshot_revalidation=stale,
            rollback_change_request=rollback.model_dump(mode='json'), restored_original=s.repo.current()[0] == original,
            audit=s.read(actors['auditor'], 'audit', size=100)['items'], financial_effects_executed=False)
    finally:
        x.close()


if __name__ == '__main__':
    root = Path('.local'); root.mkdir(exist_ok=True)
    engine = create_engine('sqlite:///' + (root / ('registry-admin-demo-' + uuid4().hex + '.db')).as_posix())
    try:
        print(json.dumps(run(engine), ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
