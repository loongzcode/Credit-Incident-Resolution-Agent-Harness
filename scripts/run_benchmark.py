"""Reproducible synthetic benchmark. No live provider unless explicitly authorized by flags."""
import argparse
import json
import os
import secrets
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from credit_harness.persistence.store import open_engine
from credit_harness.benchmark.dataset import make_dataset,seed_case_ids
from credit_harness.benchmark.models import SystemUnderTest as S,Track,BenchmarkRun,BENCHMARK_SCHEMA_VERSION,DATASET_VERSION
from credit_harness.benchmark.runner import run_case,seed_memory
from credit_harness.benchmark.report import write_report,markdown
from credit_harness.benchmark.live import live_enabled,LiveBenchmarkModel
from credit_harness.planner.models import PLANNER_POLICY_VERSION
from credit_harness.evaluation.contract import EVALUATION_POLICY_VERSION,VERIFICATION_CONTRACT_VERSION
from credit_harness.memory.skills import general_investigation_skill


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("offline","live"),default="offline")
    parser.add_argument("--live-llm",action="store_true")
    parser.add_argument("--systems",default=",".join(S))
    parser.add_argument("--seed",type=int,default=20260912)
    parser.add_argument("--repetitions",type=int)
    parser.add_argument("--output",type=Path,default=Path("benchmark"))
    parser.add_argument("--limit",type=int,help="Development subset; explicitly marked in manifest")
    args=parser.parse_args(argv)
    live=live_enabled(args.mode,args.live_llm)
    if live and (not os.environ.get("OPENAI_API_KEY") or not os.environ.get("PLANNER_MODEL")):
        print("SKIP: live benchmark requires OPENAI_API_KEY and PLANNER_MODEL; no requests made.")
        return 0
    repetitions=args.repetitions or (3 if live else 1)
    if repetitions<1 or live and not 3<=repetitions<=5:
        parser.error("live repetitions must be 3–5; offline repetitions must be positive")
    systems=tuple(S(s) for s in args.systems.split(","))
    dataset=make_dataset(args.seed)
    if args.limit:
        dataset=dataset[:args.limit]
    args.output.mkdir(parents=True,exist_ok=True)
    runs=[]
    with TemporaryDirectory(prefix="credit-benchmark-") as directory, patch.dict(os.environ,
            {"CAPABILITY_SIGNING_SECRET":secrets.token_urlsafe(48)}):
        engine=open_engine("sqlite:///"+str(Path(directory)/"benchmark.db"))
        try:
            experiences=seed_memory(engine,args.seed)
            # Append after EVERY run: interruption or provider failure cannot erase previous runs.
            raw_path=args.output/"raw-runs.jsonl"
            if raw_path.exists():
                parser.error("output already contains raw runs; choose a new --output directory")
            for index in range(repetitions):
                for spec in dataset:
                    for track in Track:
                        for system in systems:
                            try:
                                model=LiveBenchmarkModel() if live and system!=S.SOP else None
                                run=run_case(engine,spec,system,track,seed=args.seed,run_index=index,model=model,experiences=experiences)
                            except Exception as exc:
                                run=BenchmarkRun(benchmark_case_id=spec.benchmark_case_id,case_id="UNAVAILABLE",
                                    system_under_test=system,track=track,run_index=index,initial_case_signature={},
                                    final_case_status="UNAVAILABLE",stop_reason="BENCHMARK_ERROR",error_code=type(exc).__name__)
                            runs.append(run)
                            with raw_path.open("a",encoding="utf-8") as stream:
                                stream.write(run.model_dump_json()+"\n")
                            print(f"{len(runs)} {system} {track} {spec.benchmark_case_id}: {run.final_case_status} {run.error_code or ''}",flush=True)
            try:
                commit=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
            except (OSError,subprocess.CalledProcessError):
                commit=None
            source=sha256()
            for p in sorted(Path("src").rglob("*.py")):
                source.update(p.as_posix().encode()); source.update(p.read_bytes())
            skill=general_investigation_skill()
            manifest=dict(benchmark_schema_version=BENCHMARK_SCHEMA_VERSION,dataset_version=DATASET_VERSION,
                source_commit=commit,source_tree_hash=source.hexdigest(),seed=args.seed,repetitions=repetitions,
                mode="OPTIONAL_LIVE_LLM" if live else "OFFLINE_DETERMINISTIC",live_status="EXECUTED" if live else "NOT RUN",
                model_provider="openai" if live else "fake",model_name=os.environ.get("PLANNER_MODEL") if live else "context-driven-investigation-v1",
                provider_config=dict(max_output_tokens=4000,timeout_seconds=30,max_retries=0,temperature="provider default"),
                planner_policy_version=PLANNER_POLICY_VERSION,evaluation_policy_version=EVALUATION_POLICY_VERSION,
                verification_contract_version=VERIFICATION_CONTRACT_VERSION,
                skill_versions=[dict(skill_id=skill.skill_id,version=skill.version)],experience_ids=[e.experience_id for e in experiences],
                memory_seed_case_ids=seed_case_ids(args.seed),held_out_case_count=len(dataset),development_subset=bool(args.limit),
                reproduction="Fixed dataset and semantic metrics; durable UUIDs/signatures/report IDs are intentionally fresh per run")
            summary=write_report(args.output,dataset,runs,manifest)
            print(markdown(summary))
            return 1 if summary["benchmark_status"]=="SAFETY_FAIL" or any(r.stop_reason=="BENCHMARK_ERROR" for r in runs) else 0
        finally:
            engine.dispose()


if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
