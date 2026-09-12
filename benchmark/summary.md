# Synthetic benchmark — measured results

Safety gate: **SAFETY_PASS**
Runs: 288. Live LLM: NOT RUN.

| System / track | N | Verified closure | FAIL | INCONCLUSIVE | Median investigation reads | Errors / blocked |
|---|---:|---:|---:|---:|---:|---:|
| agent-cold/end-to-end | 36 | 2 | 7 | 27 | 8.0 | 0 |
| agent-cold/investigation | 36 | 0 | 9 | 27 | 8.0 | 0 |
| agent-memory/end-to-end | 36 | 2 | 7 | 27 | 8.0 | 0 |
| agent-memory/investigation | 36 | 0 | 9 | 27 | 8.0 | 0 |
| checklist/end-to-end | 36 | 2 | 8 | 26 | 9.0 | 2 |
| checklist/investigation | 36 | 0 | 9 | 25 | 9.0 | 2 |
| sop/end-to-end | 36 | 8 | 8 | 20 | 9.0 | 2 |
| sop/investigation | 36 | 0 | 9 | 25 | 9.0 | 2 |

All denominators, null/unmeasured values, paired deltas and Wilson intervals are in summary.json.
Intervals describe benchmark sampling uncertainty, never business truth probability.
Offline fake adapters test mechanics; these results do not establish live LLM superiority.
Safety failures cannot be averaged away. No single aggregate quality score is defined.
