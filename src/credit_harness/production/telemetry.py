"""Allowlisted JSON events and database-backed, low-cardinality metrics."""
import json
import logging
import re
from collections import Counter
from threading import Lock
from sqlalchemy import select, func
from sqlalchemy.orm import Session

SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


def sanitize_log(values):
    clean = {}
    for key in ("event", "component", "severity", "error_code"):
        value = values.get(key)
        if isinstance(value, str) and SAFE_CODE.fullmatch(value) and not re.search(r"\d{11}|sk-", value):
            clean[key] = value
    value = values.get("request_id")
    if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value):
        clean["request_id"] = value
    duration = values.get("duration")
    if type(duration) in (int, float) and 0 <= duration <= 86400:
        clean["duration"] = duration
    return clean


class SafeJSONFormatter(logging.Formatter):
    def format(self, record):
        # Never format getMessage(), args, exc_info, request URLs or provider bodies.
        return json.dumps(sanitize_log({**record.__dict__, "severity": record.levelname,
            "event": getattr(record, "event", "library_event")}), ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter())
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.INFO)


class OperationalMetrics:
    def __init__(self):
        self.values = Counter(dict(frame_stale_total=0, frame_build_latency_seconds_sum=0,
            frame_build_latency_seconds_count=0))
        self.lock = Lock()

    def observe(self, name, value=1):
        if name not in {"frame_stale_total", "frame_build_latency_seconds_sum", "frame_build_latency_seconds_count"}:
            raise ValueError("unknown metric")
        with self.lock:
            self.values[name] += value

    def render(self, engine, tenant):
        from credit_harness.cases.tables import CaseRow, CaseCallRow
        from credit_harness.authorization.tables import EffectRow
        from credit_harness.recovery.tables import AgentCheckpointRow, EffectRecoveryAttemptRow
        from credit_harness.evaluation.tables import EvaluationReportRow
        from credit_harness.retrieval.tables import IndexJobRow
        from .tables import RetrievalMetricRow
        with Session(engine) as session:
            def count(table, *conditions):
                return session.scalar(select(func.count()).select_from(table).where(*conditions))
            cases = select(CaseRow.case_id).where(CaseRow.tenant_id == tenant)
            effects = select(EffectRow.effect_id).where(EffectRow.case_id.in_(cases))
            values = dict(self.values)
            values.update(agent_runs_total=count(AgentCheckpointRow, AgentCheckpointRow.case_id.in_(cases)),
                tool_calls_total=count(CaseCallRow, CaseCallRow.case_id.in_(cases)),
                case_waiting_total=count(CaseRow, CaseRow.tenant_id == tenant, CaseRow.status == "WAITING"),
                case_escalated_total=count(CaseRow, CaseRow.tenant_id == tenant, CaseRow.status == "ESCALATED"),
                unknown_effect_total=count(EffectRow, EffectRow.case_id.in_(cases), EffectRow.status == "UNKNOWN"),
                recovery_attempt_total=count(EffectRecoveryAttemptRow, EffectRecoveryAttemptRow.effect_id.in_(effects)),
                planner_failures_total=count(AgentCheckpointRow, AgentCheckpointRow.case_id.in_(cases),
                    AgentCheckpointRow.stop_status == "PLANNER_UNAVAILABLE"))
            for verdict in ("PASS", "FAIL", "INCONCLUSIVE"):
                values["evaluation_" + verdict.lower() + "_total"] = count(EvaluationReportRow,
                    EvaluationReportRow.case_id.in_(cases), EvaluationReportRow.payload["overall_verdict"].as_string() == verdict)
            for state in ("PENDING", "FAILED"):
                values["index_job_" + state.lower()] = count(IndexJobRow, IndexJobRow.tenant_id == tenant, IndexJobRow.status == state)
            retrieval = session.get(RetrievalMetricRow, tenant)
            values["retrieval_latency_seconds_sum"] = retrieval.latency_sum_seconds if retrieval else 0
            values["retrieval_latency_seconds_count"] = retrieval.latency_count if retrieval else 0
        # Current-state counts are gauges, even legacy requested names end in total.
        return "".join(f"# TYPE {key} gauge\n{key} {value}\n" for key, value in sorted(values.items()))
