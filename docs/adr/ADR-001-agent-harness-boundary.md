# ADR-001 Agent Harness Boundary

## Context
模型输出会漂移，也可能受到外部数据中的命令式文本影响。调查和修复需要可复现的权限、预算与证据边界。

## Decision
模型只提出结构化候选。Harness 负责校验、硬策略、排序和执行前重新验证；所有工具经过 CaseToolExecutor。独立 Evaluator 和 Closure Service 决定业务验收及结案。

## Alternatives
直接使用 Provider Tool Calling 执行业务操作更简单，但把模型建议与执行权限混为一体。完全固定流程难以适应不同 Observation。

## Consequences
需要更多确定性契约和测试。更换 Provider 不改变业务授权，Trace 可以说明候选被拒绝的原因；不保存私有 CoT。
