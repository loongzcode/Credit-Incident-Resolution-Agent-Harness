# ADR-004 Experience != Evidence

## Context
历史类似故障能帮助选择调查方向，但不是当前订单的资金事实。组织 Skill 与历史 Experience 也有不同来源。

## Decision
只有独立验证并关闭的 Case 可发布历史 Experience。Skill 是显式审核的组织知识。二者经过 Guidance/Context eligibility，不进入当前 Evidence Store；UI 用独立 trace_trust_class 展示。

## Alternatives
将相似案例作为“高置信证据”会把历史关联错当为当前事实；完全禁用历史指导会失去可复用调查知识。

## Consequences
检索失败可降级；必须单独采集当前证据。向量排名、指导 rationale、模型建议均不赋予授权。
