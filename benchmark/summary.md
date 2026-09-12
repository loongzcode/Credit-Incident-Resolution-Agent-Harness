# Synthetic benchmark — measured results

Safety gate: **SAFETY_PASS**
Runs: 288. Live LLM: NOT RUN.

| System / track | N | Verified closure | FAIL | INCONCLUSIVE | Median investigation reads | Errors |
|---|---:|---:|---:|---:|---:|---:|
| agent-cold/end-to-end | 36 | 2 | 7 | 27 | 8.0 | 0 |
| agent-cold/investigation | 36 | 0 | 9 | 27 | 8.0 | 0 |
| agent-memory/end-to-end | 36 | 2 | 7 | 27 | 8.0 | 0 |
| agent-memory/investigation | 36 | 0 | 9 | 27 | 8.0 | 0 |
| checklist/end-to-end | 36 | 2 | 8 | 26 | 9.0 | 2 |
| checklist/investigation | 36 | 0 | 9 | 25 | 9.0 | 2 |
| sop/end-to-end | 36 | 8 | 8 | 20 | 9.0 | 2 |
| sop/investigation | 36 | 0 | 9 | 25 | 9.0 | 2 |

Benchmark schema: 2. Rates below show numerator/denominator.

| System / track | Explicit safe stop | Safe non-closure | Runs with blocked candidate | Unsafe blocked / all candidates |
|---|---:|---:|---:|---:|
| agent-cold/end-to-end | 13/21 | 21/21 | 36/36 | 30/108 |
| agent-cold/investigation | 22/32 | 32/32 | N/A (0 denominator) | N/A (0 denominator) |
| agent-memory/end-to-end | 13/21 | 21/21 | 36/36 | 30/108 |
| agent-memory/investigation | 22/32 | 32/32 | N/A (0 denominator) | N/A (0 denominator) |
| checklist/end-to-end | 0/31 | 31/31 | 36/36 | 70/108 |
| checklist/investigation | 0/32 | 32/32 | N/A (0 denominator) | N/A (0 denominator) |
| sop/end-to-end | 13/25 | 25/25 | 36/36 | 48/108 |
| sop/investigation | 13/32 | 32/32 | N/A (0 denominator) | N/A (0 denominator) |

Explicit safe stop excludes MAX_TURNS and ordinary unresolved completion; both safe-stop rates use oracle-disallowed closure runs.
Safe non-closure measures absence of incorrect closure/unsafe action, not deliberate stopping or infrastructure reliability.
Blocked/stale does not imply unsafe: unsafe counts use deterministic reject reasons and action risk, not model rationale.

All denominators, null/unmeasured values, paired deltas and Wilson intervals are in summary.json.
Intervals describe benchmark sampling uncertainty, never business truth probability.
Offline fake adapters test mechanics; these results do not establish live LLM superiority.
Safety failures cannot be averaged away. No single aggregate quality score is defined.
