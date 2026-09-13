import pytest
from datetime import timedelta
from sqlalchemy import event, select, update, delete, func
from sqlalchemy.orm import Session
from tests.test_organizational_memory import factory, context
from tests.test_hybrid_retrieval import setup, add_skill, search
from credit_harness.production.tables import SpaceAdminAuditRow, SourceGenerationRow, IndexCompletenessRow
from credit_harness.production.completeness import require_ready, reconcile_completeness
from credit_harness.retrieval.models import DocumentType, RetrievalError
from credit_harness.retrieval.tables import SkillVectorRow
from credit_harness.retrieval.embedding import DeterministicFakeEmbeddingProvider
from credit_harness.retrieval.indexer import EmbeddingIndexer


def test_vector_primary_candidates_loaded_in_single_sql(factory):
    x = factory()
    for n in range(12): add_skill(x, f'payment-{n}')
    repo, provider, _, space, _ = setup(x)
    candidates = search(repo, provider, space, context(x))
    statements = []
    def capture(conn, cursor, sql, *args): statements.append(sql)
    event.listen(x.engine, 'before_cursor_execute', capture)
    try: documents = repo.sources.batch_get(DocumentType.SKILL, candidates)
    finally: event.remove(x.engine, 'before_cursor_execute', capture)
    assert len(documents) == len(candidates) == 12
    assert len(statements) == 1


def test_retired_space_rollback_is_validated_and_audited(factory):
    x = factory(); add_skill(x)
    repo, p, worker, old, _ = setup(x)
    p2 = DeterministicFakeEmbeddingProvider(32)
    second = repo.create_space(p2, x.clock.now + timedelta(seconds=1))
    worker = EmbeddingIndexer(repo,p2,clock=lambda:x.clock.now)
    worker.reconcile(second)
    while worker.run_one(second.space_id): pass
    repo.activate_space(second.space_id)
    SpaceAdminAuditRow.__table__.create(x.engine, checkfirst=True)
    with pytest.raises(RetrievalError):
        repo.rollback_space(old.space_id,p2,actor='synthetic-admin',now=x.clock.now)
    repo.rollback_space(old.space_id, p, actor='synthetic-admin', now=x.clock.now)
    assert repo.active_space().space_id == old.space_id
    with Session(x.engine) as s:
        audit = s.scalar(select(SpaceAdminAuditRow))
        assert (audit.from_space, audit.to_space) == (second.space_id, old.space_id)


def test_incomplete_retired_space_cannot_be_rolled_back(factory):
    x = factory(); add_skill(x)
    repo, p, worker, old, _ = setup(x)
    p2 = DeterministicFakeEmbeddingProvider(32)
    second = repo.create_space(p2, x.clock.now + timedelta(seconds=1))
    worker = EmbeddingIndexer(repo,p2,clock=lambda:x.clock.now)
    worker.reconcile(second)
    while worker.run_one(second.space_id): pass
    repo.activate_space(second.space_id)
    with x.engine.begin() as c: c.execute(delete(SkillVectorRow).where(SkillVectorRow.embedding_space_id==old.space_id))
    with pytest.raises(RetrievalError): repo.rollback_space(old.space_id,p,actor='synthetic-admin',now=x.clock.now)
    assert repo.active_space().space_id == second.space_id


def test_completeness_admission_is_constant_reads_and_fails_on_generation_change(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x)
    for table in (SourceGenerationRow, IndexCompletenessRow): table.__table__.create(x.engine, checkfirst=True)
    reconcile_completeness(repo,space)
    statements = []
    def capture(conn,cursor,sql,*args): statements.append(sql)
    event.listen(x.engine,'before_cursor_execute',capture)
    try:
        with Session(x.engine) as s: require_ready(s,x.cases.tenant_id,space.space_id)
    finally: event.remove(x.engine,'before_cursor_execute',capture)
    assert len(statements) == 4 and all('count(' not in q.lower() for q in statements)
    with Session(x.engine) as s, s.begin():
        s.add(SourceGenerationRow(tenant_id=x.cases.tenant_id,document_type='SKILL',generation=1))
    with Session(x.engine) as s, pytest.raises(RetrievalError): require_ready(s,x.cases.tenant_id,space.space_id)
