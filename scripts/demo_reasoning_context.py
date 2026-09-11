"""Trusted manual investigation followed by a pure context projection; no LLM."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo_case_evidence import run_demo
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ScenarioId
from credit_harness.persistence.store import open_engine


def run_context_demo(engine, scenario=ScenarioId.S6):
    diagnostic = run_demo(engine, scenario)
    evidence = tuple(e for items in diagnostic.evidence_by_claim_type.values() for e in items)
    snapshot = ReasoningContextAssembler().build(diagnostic.case, evidence)
    return snapshot, diagnostic.case, evidence


def preview(s):
    lines = [f"CASE {s.internal_order_id}", f"SNAPSHOT {s.snapshot_id}",
             "未来 Planner 的结构化输入；本次没有 LLM 调用。", "", "TASK", s.task.goal,
             "", "SAFETY", *(v.value for v in s.safety_constraints.invariants),
             "", "FINANCIAL SUBJECT", s.financial_subject.model_dump_json() if s.financial_subject else "UNKNOWN",
             "", "PAYMENT IDENTITY", s.financial_identity.model_dump_json(indent=2),
             "", "CURRENT FACTS"]
    lines.extend(f"{f.claim_type.value} [{f.subject.identifier}; protocol={f.protocol_version}] = {f.value} ({f.freshness.value})" for f in s.current_facts)
    lines.extend(["", "ACTIVE HYPOTHESES"])
    lines.extend(f"{h.hypothesis_id.value} {h.status.value}; decisive={list(h.decisive_evidence_refs)}" for h in s.active_hypotheses)
    lines.extend(["", "RESOLVED HYPOTHESES"])
    lines.extend(f"{h.hypothesis_id.value} {h.status.value}" for h in s.resolved_hypotheses_summary)
    lines.extend(["", "OPEN GAPS"])
    lines.extend(f"{g.priority.value} {g.gap_id}" for g in s.open_evidence_gaps)
    lines.extend(["", "TOOLS AVAILABLE (alphabetical; no selection)", *(t.tool_name.value for t in s.available_tools),
                  "", "HISTORY DIGEST", s.history_digest.model_dump_json(indent=2),
                  "", "BUDGET USAGE", s.context_budget_usage.model_dump_json(indent=2),
                  "", "SELECTED / OMITTED", s.omitted_evidence_summary.model_dump_json(indent=2),
                  "", "Preview 仅用于检查；结构化 JSON Snapshot 是真相。"])
    return "\n".join(lines) + "\n"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/context-demo-{uuid4().hex}.db")
    try:
        snapshot, case, evidence = run_context_demo(engine, ScenarioId(args.scenario))
        human = preview(snapshot)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(snapshot.model_dump_json() + "\n", encoding="utf-8")
            args.output.with_suffix(".preview.txt").write_text(human, encoding="utf-8")
            # Audit sidecar is NOT a model context; no Observation objects are copied.
            args.output.with_suffix(".inputs.json").write_text(json.dumps({
                "case": case.model_dump(mode="json"),
                "evidence": [e.model_dump(mode="json") for e in evidence],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(human)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
