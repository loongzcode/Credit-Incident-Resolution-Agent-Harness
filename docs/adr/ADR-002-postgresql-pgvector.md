# ADR-002 PostgreSQL + pgvector

## Context
核心数据需要事务、唯一约束、租约和恢复；组织知识检索需要近似向量候选，但不是新的事实库。

## Decision
PostgreSQL 保存权威业务数据，pgvector 保存可重建索引。Space 绑定模型/维度/投影版本；检索先过滤再排序，批量回主表验证。Alembic 和索引管理身份负责 DDL。

## Alternatives
独立 Vector DB 可单独扩展，但增加一致性与恢复链路。SQLite 仅作为快速工程测试后端，不模拟 PostgreSQL 锁和 HNSW 行为。

## Consequences
部署需要 pgvector；维度变更需要新 Space 和回填。删除全部向量不能改变 Evidence 或 Closure，必须能从主表重建。
