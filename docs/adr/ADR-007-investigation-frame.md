# ADR-007 Investigation Frame Consistency

## Context
逐页独立读取会混合不同 Case revision。进程内缓存和随机进程 key 无法支持多个 API 副本。

## Decision
使用一致读快照和提交后 watermark 检查；安全投影写入 PostgreSQL 短期不可变 Frame。首次写入胜出，分页验证 tenant/Case/section/TTL。游标 HMAC 支持 current+previous；显示引用采用 tenant-scoped HMAC。

## Alternatives
sticky session 无法覆盖进程故障。缓存原始数据库行会突破 UI eligibility。每页重新组装则失去一致性。

## Consequences
过期或过时必须刷新，不能静默混合页面。Cache 不是事实源；关闭展示还需完整重验 Closure 绑定。Frame 适合人工调查，不是模型 Planner Context。
