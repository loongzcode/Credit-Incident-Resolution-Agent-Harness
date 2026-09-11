"""Trusted manual investigation setup followed by a pure Evidence projection."""
import argparse
from pathlib import Path
import sys
from uuid import uuid4

# Support both `python scripts/...py` and module import by integration tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo_case_evidence import run_demo
from credit_harness.domain.enums import ScenarioId
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.persistence.store import open_engine


def run_graph_demo(engine, scenario=ScenarioId.S6):
    # Setup and manual Tool calls are outside HypothesisEngine. No raw observations
    # or scenario label cross the evaluate() boundary.
    diagnostic = run_demo(engine, scenario)
    evidence = tuple(e for group in diagnostic.evidence_by_claim_type.values() for e in group)
    return HypothesisEngine().evaluate(diagnostic.case, evidence), diagnostic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S8"], default="S6")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/hypothesis-demo-{uuid4().hex}.db")
    try:
        graph, evidence = run_graph_demo(engine, ScenarioId(args.scenario))
        print(f"case={graph.case_id} rules={graph.rule_version}")
        for h in graph.hypotheses:
            print(f"{h.hypothesis_id.value} {h.status.value}")
            print(f"  support={list(h.supporting_evidence_refs)}")
            print(f"  contradict={list(h.contradicting_evidence_refs)}")
            print(f"  decisive={list(h.decisive_evidence_refs)}")
        print("Open gaps:")
        for gap in graph.open_gaps:
            print(f"  {gap.gap_id}: {[c.value for c in gap.required_claim_types]}")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(graph.model_dump_json(indent=2) + "\n", encoding="utf-8")
            # Sidecar keeps actual input evidence available for auditing graph refs.
            args.output.with_suffix(".evidence.json").write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
