import json
from pathlib import Path
from .metrics import aggregate
from .models import BenchmarkRun


def markdown(summary):
    lines=["# Synthetic benchmark — measured results", "", f"Safety gate: **{summary['benchmark_status']}**",
           f"Runs: {summary['run_count']}. Live LLM: {summary.get('manifest',{}).get('live_status','NOT RUN')}.", "",
           "| System / track | N | Verified closure | FAIL | INCONCLUSIVE | Median investigation reads | Errors / blocked |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for key,g in summary["groups"].items():
        lines.append(f"| {key} | {g['count']} | {g['success_count']} | {g['failure_count']} | {g['inconclusive_count']} | {g['investigation']['tool_calls']['median']} | {g['infrastructure_or_policy_error_count']} |")
    lines += ["", "All denominators, null/unmeasured values, paired deltas and Wilson intervals are in summary.json.",
              "Intervals describe benchmark sampling uncertainty, never business truth probability.",
              "Offline fake adapters test mechanics; these results do not establish live LLM superiority.",
              "Safety failures cannot be averaged away. No single aggregate quality score is defined."]
    return "\n".join(lines)+"\n"


def write_report(output, dataset, runs, manifest):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    summary=aggregate(runs)
    summary["manifest"]=manifest
    (output/"raw-runs.jsonl").write_text("".join(r.model_dump_json()+"\n" for r in runs),encoding="utf-8")
    (output/"cases.json").write_text(json.dumps([d.model_dump(mode="json") for d in dataset],ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (output/"summary.md").write_text(markdown(summary),encoding="utf-8")
    return summary


def recompute(path):
    return aggregate([BenchmarkRun.model_validate_json(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line])
