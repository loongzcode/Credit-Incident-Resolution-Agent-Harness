import os
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from credit_harness.production.schema import metadata, HEAD


@pytest.fixture
def migration_engine(tmp_path, request):
    if not request.config.getoption('--postgres'):
        engine = create_engine('sqlite:///' + (tmp_path/'migrated.db').as_posix())
        yield engine
        engine.dispose()
        return
    base = create_engine(os.environ['TEST_POSTGRES_URL'])
    schema = 'migration_test_' + uuid4().hex
    with base.begin() as c: c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    try:
        yield base.execution_options(schema_translate_map={None:schema})
    finally:
        with base.begin() as c: c.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        base.dispose()


def upgrade(engine, revision='head'):
    config = Config('alembic.ini')
    with engine.begin() as c:
        config.attributes['connection'] = c
        command.upgrade(config, revision)


def test_migration_fresh_install_all_tables(migration_engine):
    upgrade(migration_engine)
    schema = migration_engine.get_execution_options().get('schema_translate_map',{}).get(None)
    assert set(metadata().tables) <= set(inspect(migration_engine).get_table_names(schema=schema))
    with migration_engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == HEAD
        assert c.scalar(text('SELECT COUNT(*) FROM schema_migration_audit')) >= 3
        if c.dialect.name == 'postgresql':
            assert c.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))


def test_migration_expand_backfill_contract_preserves_old_rows(migration_engine):
    upgrade(migration_engine, '0016_baseline')
    with migration_engine.begin() as c:
        c.execute(text("INSERT INTO registry_heads (tenant_id,version) VALUES ('migration-synthetic',NULL)"))
    upgrade(migration_engine, '0017_expand')
    schema = migration_engine.get_execution_options().get('schema_translate_map',{}).get(None)
    assert next(c for c in inspect(migration_engine).get_columns('investigation_safe_traces',schema=schema)
        if c['name']=='projection_version')['nullable']
    upgrade(migration_engine)
    assert not next(c for c in inspect(migration_engine).get_columns('investigation_safe_traces',schema=schema)
        if c['name']=='projection_version')['nullable']
    with migration_engine.connect() as c:
        assert c.scalar(text("SELECT COUNT(*) FROM registry_heads WHERE tenant_id='migration-synthetic'")) == 1
    upgrade(migration_engine)  # restart after successful migration is a no-op


def test_runtime_cannot_create_embedding_space(migration_engine):
    upgrade(migration_engine)
    from credit_harness.retrieval.repository import VectorRepository
    from credit_harness.retrieval.embedding import DeterministicFakeEmbeddingProvider
    from credit_harness.retrieval.models import RetrievalError
    from types import SimpleNamespace
    repo = VectorRepository(SimpleNamespace(engine=migration_engine.execution_options(production_runtime=True)))
    with pytest.raises(RetrievalError): repo.create_space(DeterministicFakeEmbeddingProvider(), None)


def test_source_generation_changes_on_committed_publication_and_revocation(migration_engine):
    upgrade(migration_engine)
    from credit_harness.memory.tables import SkillRow
    from credit_harness.production.tables import SourceGenerationRow
    from sqlalchemy import select, update, delete
    def generation():
        with migration_engine.connect() as c:
            return c.scalar(select(SourceGenerationRow.generation).where(SourceGenerationRow.tenant_id=='demo'))
    with migration_engine.begin() as c:
        c.execute(SkillRow.__table__.insert().values(tenant_id='demo',skill_id='synthetic',version='1',status='ACTIVE',content_hash='0'*64,payload={}))
    assert generation()==1
    with migration_engine.begin() as c: c.execute(update(SkillRow).values(status='RETIRED'))
    assert generation()==2
    with migration_engine.begin() as c: c.execute(delete(SkillRow))
    assert generation()==3
