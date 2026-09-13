"""Long-running processes; each tick delegates to existing fenced domain workers."""
import argparse
import logging
import signal
import threading
from time import monotonic, time
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.orchestration.models import WorkType
from credit_harness.retrieval.indexer import insert_for
from .tables import WorkerHeartbeatRow
from .telemetry import configure_logging


class WorkerLoop:
    def __init__(self, tick, *, interval=2, grace=35, heartbeat=lambda status: None):
        self.tick, self.interval, self.grace, self.heartbeat = tick, interval, grace, heartbeat
        self.stopping = threading.Event()

    def shutdown(self, *_):
        self.stopping.set()

    def run(self):
        while not self.stopping.is_set():
            finished = threading.Event()
            def bounded_tick():
                try:
                    self.tick()
                except Exception:
                    logging.getLogger("harness").error("", extra={"event": "worker_tick_failed", "component": "worker", "error_code": "TICK_FAILED"})
                finally:
                    finished.set()
            self.heartbeat("WORKING")
            worker = threading.Thread(target=bounded_tick, daemon=True)
            worker.start()
            last_heartbeat = monotonic()
            while not finished.wait(.2):
                if self.stopping.is_set():
                    finished.wait(self.grace)
                    # The CLI exits the process after this return. Domain leases
                    # remain durable; there is no retry or forced lease release.
                    self.heartbeat("STOPPED")
                    return
                if monotonic() - last_heartbeat >= 5:
                    self.heartbeat("WORKING")
                    last_heartbeat = monotonic()
            self.heartbeat("IDLE")
            self.stopping.wait(self.interval)
        self.heartbeat("STOPPED")


class DeploymentTicks:
    def __init__(self, components, settings, worker_id):
        self.c, self.settings, self.worker_id = components, settings, worker_id
        self.next_reconcile = 0.

    def operational(self, kind):
        if kind == 'recovery':
            from credit_harness.orchestration.repository import lock_case
            from credit_harness.orchestration.effect_work import ensure_recovery_work
            from credit_harness.cases.repository import utc_now
            now = utc_now()
            repository = self.c.recovery.repository
            # Repair the crash window before an effect-bound WorkItem exists.
            # This only schedules lookup; it has no side-effect dispatch client.
            for effect_id in repository.list_recoverable_effects(now, limit=100):
                effect = repository.authorization.get_ledger(effect_id)
                with Session(self.c.engine) as session, session.begin():
                    case = lock_case(session, self.c.cases, effect.case_id)
                    ensure_recovery_work(session, case, effect_id, now)
        types = {"agent": {WorkType.INVESTIGATION_RESUME, WorkType.OPERATOR_FOLLOWUP},
                 "orchestration": {WorkType.VERIFICATION_REQUIRED},
                 "recovery": {WorkType.RECOVERY_RECHECK}}[kind]
        for work_id in self.c.work.poll_due_work():
            if self.c.work.get(work_id).work_type in types:
                claim = self.c.work.claim(work_id, self.worker_id)
                if claim:
                    return self.c.orchestrator.process(claim)

    def embedding(self):
        from credit_harness.production.completeness import reconcile_completeness
        from credit_harness.retrieval.tables import SpaceRow
        from credit_harness.retrieval.models import RetrievalError
        with Session(self.c.engine) as session:
            spaces = [self.c.vector.read_space(r) for r in session.scalars(select(SpaceRow).where(SpaceRow.status.in_(["ACTIVE", "BUILDING"])))]
        due = monotonic() >= self.next_reconcile
        for space in spaces:
            # A deployment uses the provider for its own model/dimension. Other
            # BUILDING spaces are maintained by their matching worker rollout.
            try:
                self.c.vector.validate_provider(space, self.c.indexer.provider)
            except RetrievalError:
                continue
            if due:
                self.c.indexer.reconcile(space)
            self.c.indexer.run_one(space.space_id)
            if due:
                try:
                    reconcile_completeness(self.c.vector, space)
                except RetrievalError:
                    pass  # Pending backfill remains unavailable, never fake READY.
        if due:
            self.next_reconcile = monotonic()+self.settings.reconcile_seconds

    def heartbeat(self, kind, status):
        values = dict(worker_id=self.worker_id, tenant_id=self.settings.tenant, kind=kind, updated_at=time(), status=status)
        with self.c.engine.begin() as connection:
            connection.execute(insert_for(self.c.engine, WorkerHeartbeatRow).values(**values).on_conflict_do_update(
                index_elements=["worker_id"], set_={"updated_at": values["updated_at"], "status": status}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("agent", "orchestration", "recovery", "embedding"))
    args = parser.parse_args()
    configure_logging()
    from .settings import WORKER_SETTINGS
    from .wiring import create_components
    settings = WORKER_SETTINGS[args.kind].from_env()
    components = create_components(settings, args.kind)
    ticks = DeploymentTicks(components, settings, uuid4().hex)
    tick = ticks.embedding if args.kind == "embedding" else lambda: ticks.operational(args.kind)
    loop = WorkerLoop(tick, interval=settings.worker_tick_seconds, grace=settings.shutdown_grace_seconds,
        heartbeat=lambda status: ticks.heartbeat(args.kind, status))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, loop.shutdown)
    loop.run()
    # No non-daemon business worker may continue after the process is stopped.


if __name__ == "__main__":
    try:
        main()
    except Exception:
        configure_logging()
        logging.getLogger("harness").critical("", extra={"event": "worker_stopped", "error_code": "STARTUP_OR_DEPENDENCY_FAILURE"})
        raise SystemExit(1) from None
