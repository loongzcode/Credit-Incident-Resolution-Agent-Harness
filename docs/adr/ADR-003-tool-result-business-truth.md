# ADR-003 Tool Result != Business Truth

## Context
HTTP Timeout 可能发生在资金已成功之后；资金方 SUCCESS 也可能只表示借据建立。

## Decision
Observation 表示工具看到什么，Evidence 是确定性提取的可追溯事实，业务结论由 Evidence Contract/Evaluator 决定。Timeout、NOT_FOUND 和 stale 不转成资金 FAILED。

## Alternatives
把工具 status 直接映射到业务 status 实现简单，但可能造成重复放款或错误结案。

## Consequences
UNKNOWN 是必须长期支持的状态。需要事件/版本关联、资金身份契约和来源适用性，不以“连续查询失败”证明未执行。
