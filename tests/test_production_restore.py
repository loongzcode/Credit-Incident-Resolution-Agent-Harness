"""Synthetic full database backup, restore and disposable vector rebuild."""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, delete
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from tests.test_organizational_memory import factory
from tests.test_hybrid_retrieval import setup, add_skill
from credit_harness.context.budget import digest
from credit_harness.production.schema import metadata
from credit_harness.cases.repository import CaseRepository
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.memory.skills import SQLSkillRepository
from credit_harness.memory.repository import SQLExperienceRepository
from credit_harness.retrieval.source import MemorySources
from credit_harness.retrieval.repository import PgVectorRepository, ExactVectorTestRepository
from credit_harness.retrieval.tables import SkillVectorRow, ExperienceVectorRow
from credit_harness.retrieval.indexer import EmbeddingIndexer
from credit_harness.registry.repository import RegistryAdmin, RegistryRepository
from credit_harness.registry.fixtures import synthetic_registry


def core_digest(engine):
    excluded = {'skill_vector_index','experience_vector_index','retrieval_embedding_index_jobs',
        'retrieval_completeness','retrieval_source_generations','investigation_frames'}
    from sqlalchemy import inspect
    schema = engine.get_execution_options().get('schema_translate_map',{}).get(None)
    existing = set(inspect(engine).get_table_names(schema=schema))
    with engine.connect() as c:
        return {name:digest(sorted([dict(r) for r in c.execute(select(table)).mappings()],key=lambda r:digest(r)))
            for name,table in metadata().tables.items() if name in existing and name not in excluded}


def test_synthetic_backup_restore_and_vector_rebuild(factory, tmp_path):
    x = factory(closed=True); x.publisher.publish(x.case.case_id); add_skill(x)
    repo,p,worker,space,_ = setup(x)
    admin = RegistryAdmin(RegistryRepository(x.engine,'demo'))
    version = admin.register(synthetic_registry('demo'),actor='synthetic-restore')
    admin.activate(version,expected_version=None,actor='synthetic-restore')
    # Another Case retains the originally UNKNOWN synthetic effect's recovery audit.
    y = factory(case_id='CASE-RESTORE-UNKNOWN'); y.read(); effect = y.remediate(timeout=True)
    from credit_harness.recovery.repository import RecoveryRepository
    from credit_harness.recovery.service import SideEffectRecoveryCoordinator
    from credit_harness.recovery.models import RecoveryPolicy
    from credit_harness.authorization.store import SQLApprovalStore
    from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
    resolver = SyntheticEffectStatusResolver(y.engine,'demo',clock=lambda:y.clock.now)
    recovery = SideEffectRecoveryCoordinator(RecoveryRepository(SQLApprovalStore(y.cases),resolver.capability,
        policy=RecoveryPolicy(grace_seconds=0)),resolver,clock=lambda:y.clock.now)
    recovery.recover(effect.effect_id)
    before = core_digest(x.engine)
    base = None; db = None
    if x.engine.dialect.name == 'sqlite':
        target = tmp_path/'restored.db'
        with sqlite3.connect(x.engine.url.database) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        restored = create_engine('sqlite:///'+target.as_posix())
    else:
        url = make_url(os.environ['TEST_POSTGRES_URL']); db = 'restore_test_'+uuid4().hex
        base = create_engine(url,isolation_level='AUTOCOMMIT')
        with base.connect() as c: c.exec_driver_sql(f'CREATE DATABASE "{db}"')
        env = dict(os.environ,PGHOST=url.host,PGPORT=str(url.port or 5432),PGUSER=url.username,PGDATABASE=url.database)
        if url.password: env['PGPASSWORD'] = url.password
        def pgtool(name):
            found = shutil.which(name)
            if found: return found
            root = Path(os.environ.get('PG_BIN','C:/App/Env/PostgreSQL/bin'))
            path = root/(name+'.exe')
            if not path.exists(): pytest.fail('PostgreSQL backup client tools required')
            return str(path)
        schema = x.engine.get_execution_options()['schema_translate_map'][None]
        dump = tmp_path/'synthetic.dump'
        subprocess.run([pgtool('pg_dump'),'--format=custom','--no-owner','--schema='+schema,'--file='+str(dump)],
            env=env,check=True,capture_output=True,timeout=60)
        restored_base = create_engine(url.set(database=db))
        with restored_base.begin() as c: c.exec_driver_sql('CREATE EXTENSION vector')
        subprocess.run([pgtool('pg_restore'),'--no-owner','--exit-on-error','--dbname='+db,str(dump)],
            env=env,check=True,capture_output=True,timeout=60)
        restored = restored_base.execution_options(schema_translate_map={None:schema})
    try:
        assert core_digest(restored) == before
        cases = CaseRepository(restored,'demo')
        assert cases.get(x.case.case_id).status.value == 'CLOSED_VERIFIED'
        assert EvidenceRepository(cases).list(x.case.case_id)
        with restored.begin() as c:
            c.execute(delete(SkillVectorRow)); c.execute(delete(ExperienceVectorRow))
        assert core_digest(restored) == before
        sources = MemorySources(SQLSkillRepository(restored,'demo'),SQLExperienceRepository(cases))
        rebuilt = (PgVectorRepository if restored.dialect.name=='postgresql' else ExactVectorTestRepository)(sources)
        indexer = EmbeddingIndexer(rebuilt,p,clock=lambda:x.clock.now)
        indexer.reconcile(rebuilt.active_space())
        while indexer.run_one(space.space_id): pass
        with Session(restored) as s: rebuilt.assert_complete(s,rebuilt.active_space())
        assert core_digest(restored) == before
    finally:
        restored.dispose()
        if base is not None:
            # Only a database with this test's generated name can be removed.
            assert db.startswith('restore_test_') and len(db)==45
            with base.connect() as c: c.exec_driver_sql(f'DROP DATABASE "{db}" WITH (FORCE)')
            base.dispose()
