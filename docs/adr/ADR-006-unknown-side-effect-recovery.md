# ADR-006 Unknown Side Effect Recovery

## Context
网络丢包、Worker crash 或超时可能使执行结果未知。创建第二个 effect 或放款意图不能作为恢复策略。

## Decision
保存原 effect identity、dispatch correlation 与 durable ledger；恢复只查询原 effect，验证 receipt 和身份后推进状态。Work 与 effect 绑定，lease/fence 阻止旧 Worker 提交。

## Alternatives
按异常重试 dispatch 无法区分未发送与已执行；把 UNKNOWN 变成 FAILED 会制造重复效果。

## Consequences
有些 Case 需要长期等待或人工升级。Shutdown 不盲目重放，APPLIED 仍不等于 VERIFIED；当前部署只使用 synthetic adapter。
