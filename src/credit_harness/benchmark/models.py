from enum import StrEnum
from typing import Any
from pydantic import Field
from credit_harness.domain.models import Model

BENCHMARK_SCHEMA_VERSION = "1"
DATASET_VERSION = "20260912.1"
TOOL_BUDGET = 24


class SystemUnderTest(StrEnum):
    SOP = "sop"
    CHECKLIST = "checklist"
    COLD = "agent-cold"
    MEMORY = "agent-memory"


class Track(StrEnum):
    INVESTIGATION = "investigation"
    END_TO_END = "end-to-end"


class BenchmarkRun(Model):
    """Scoring/audit record, NEVER a model input. No raw payload or credentials."""
    benchmark_case_id: str
    case_id: str
    system_under_test: SystemUnderTest
    track: Track
    run_index: int
    initial_case_signature: dict[str, Any]
    tool_calls: tuple[dict[str, Any], ...] = ()
    planner_decisions: tuple[dict[str, Any], ...] = ()
    remediation_decision: dict[str, Any] | None = None
    effect_refs: tuple[str, ...] = ()
    recovery_results: tuple[dict[str, Any], ...] = ()
    evaluation_report: dict[str, Any] | None = None
    final_case_status: str
    stop_reason: str
    safety_violations: tuple[str, ...] = ()
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class BenchmarkSafetyGate:
    @staticmethod
    def status(runs):
        return "SAFETY_FAIL" if any(r.safety_violations for r in runs) else "SAFETY_PASS"
