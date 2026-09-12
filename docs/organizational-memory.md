# Organizational Memory — Step 11

Past Experience **不是** Current Business Truth。组织经验帮助调查者优先取得有价值的当前证据；不能证明第 101 笔订单已经放款，即使前 100 个类似 Case 都是 SETTLED。

## Tool、Skill、Experience

| 对象 | 回答 | 边界 |
|---|---|---|
| Tool / Capability | 当前能查什么 | scope、budget、Tool Gateway 决定实际权限 |
| Skill | 遇到一类事故应关注什么调查目标和证据 | 有版本的组织指导，不是可执行 DAG |
| Verified Experience | 过去某个已验证关闭 Case 实际发生了什么 | 历史投影，不能作为当前 Evidence |

没有 Memory 时调查仍正常运行。有 Memory 时仍保留多个假设、当前 Evidence Gap、独立身份契约与安全策略。冷启动不依赖历史数据库可用性。

## 实现目录与入口

`src/credit_harness/memory/` 包含：

- `models.py`：Skill、Experience、IncidentSignature、Capsule、Guidance 和 Proposal 的 frozen / extra=forbid schema。
- `source.py`：已关闭来源的结构及封存指纹校验。
- `experience.py`：显式、确定性的 `VerifiedExperiencePublisher.publish(case_id)`。
- `repository.py` / `tables.py`：tenant-scoped Experience、Skill、追加审计表。
- `skills.py`：`SkillRepository` Protocol、SQL 实现、可信激活/退役、确定性组合与通用 Skill fixture。
- `retrieval.py` / `guidance.py`：结构化检索、有界投影与独立 Guidance Bundle。
- `aggregation.py`：经验计数、观察到的 yield、待审阅 Skill 改进提案。

先沿用 Case / Authorization / Recovery / Evaluation schema bootstrap，再显式调用 `create_memory_schema(engine)`。这是可选模块，基础调查 Runtime 不依赖 Memory 表存在。没有新的 Planner Tool、Agent 管理端点或后台自动发布任务。

## 唯一发布入口与 Memory Poisoning Boundary

发布必须同时满足：

1. tenant-scoped Case 的持久状态为 `CLOSED_VERIFIED`。
2. 存在 Step 10 ClosureRow 及其指向的 EvaluationReportRow。
3. report verdict 是 PASS，Case / tenant / evaluation run / report / snapshot 全部绑定一致。
4. Report、Snapshot、Closure 的内容身份均可重算。
5. 当前封存的 Evidence、Origins、Observation receipts、CaseCall history、Ledger 和 Recovery 指纹与关闭报告一致。
6. 报告引用的 Evidence 存在，并且源 Case 只经历了关闭所规定的 status/revision 变化。

Publisher 持有同一 Case 锁，以事务完成验证及唯一经验插入。同 Closure + schema version 有唯一约束；两个 Publisher 并发只产生一条记录。普通 CLOSED、WAITING、ESCALATED、FAIL、INCONCLUSIVE、APPLIED 未验证、只有 PASS 没有 Closure 全部拒绝。

这里校验的是**历史结果的来源完整性**，不重新判卷：不调用 IndependentEvaluator，不重新检查今天的 TTL，不改变历史 verdict。哈希是完整性与绑定机制，不是数据库管理员不可伪造的数字签名；持久库的写权限仍属于可信 Runtime。真实生产的独立防篡改存储、签名审计和 IAM 不在本阶段实现。

Step 10 的小 hardening：`SQLApprovalStore.decide_approval` 在同一 Case 锁下先检查 terminal 状态。CLOSED / CLOSED_VERIFIED 禁止所有普通批准、拒绝及撤销决定，包括旧 PENDING 与旧 APPROVED。没有改变 Closure CAS 语义；未来独立应急撤销接口需另行设计。

## VerifiedIncidentExperience schema 与不可变性

经验绑定 `experience_id`、`source_case_id`、`closure_id`、`report_id`、`verification_snapshot_id`、`tenant_id`、safe order token、outcome path、结构化 signature、partner/protocol context、症状、历史确认假设、调查序列、证据类型、修复动作、效果结果、恢复模式、安全经验和版本 provenance。

`experience_id` 是整个结构化 body（排除自身 ID）的内容哈希，body 已包含 Closure、Report 与 schema version。`created_at` 使用来源关闭时间，实际发布时间另在追加审计中记录；重复发布不会因墙上时钟或规则升级改变 ID。

原 payload 不更新、不删除。ACTIVE / REVOKED 是独立管理状态，撤销只改变检索适用性，不改写当时的业务结果。撤销后重新 publish 返回原对象，不会悄悄恢复 ACTIVE。Schema 升级必须形成新版本投影，不能覆盖旧记录。

## Signature、调查路径与历史假设

`IncidentSignature` 使用可选 Enum / bool / 结构化版本字段：HTTP transport、Fund status、首次观察到的 payment finality、确定性 identity state、Gateway、Consumer、schema mismatch、Asset、Guarantee、Accounting，以及可选 partner/product/protocol。

历史 signature 中每个症状取调查路径上首次 CURRENT + COMPLETE 的相应 Evidence；`payment_finality_at_detection` 表达首次实际观察到的支付结果，**不声称受理 Case 当时已经知道**。Unknown 不填补。当前 Signature 从经过 seal 校验的 ReasoningContextSnapshot 取得，拒绝调用者提供与 Snapshot 不符的资金状态。

当前 Case 没有明确 funding/asset partner role 或 product 的输入字段；不能从 JD、ScenarioId 或 protocol subject 的名字猜角色。因此本版这些字段为 None。Scope schema 与组合器已支持 General / Partner / Product overlay，只有存在匹配的结构化上下文时才能使用具体 scope。

调查序列严格按持久 CaseCall sequence；scope 中订单是 tenant 内哈希 token，保留版本/生效时间。EvidenceOriginRow 关联每次调用的 claim types，首次出现的 Evidence ID 计入 new evidence yield，重复关联不再次计为新 Evidence。这是 observed path，不是未来必须遵循的顺序。未生成 `ineffective_queries`，因为一个 Case 不能证明某查询普遍浪费。

历史 confirmed_hypotheses 由 HypothesisEngine 对调查序列的 Evidence 前缀确定性计算，记录首次确认时的 call sequence、decisive Evidence IDs 和规则版本。这样已修复后变为 CONSUMED 不会抹去历史上确实确认过的 H6。它不是模型生成的 Root Cause，也不会回填当前 Graph。

## 历史 Extractor Version

Publisher 读取已封存的 Evidence，不调用今天的 EvidenceExtractor。记录全部 `source_extractor_versions`、hypothesis rule version、Evidence/history 指纹以及 Evaluation policy/contract version。既有经验在重复发布时直接读取原 payload，不重跑 Hypothesis。

首次发布的历史假设是明确标注规则版本的确定性投影；它不冒称曾经持久化过一张旧版本 Graph。旧 extractor 的可执行恢复、完整 Versioned Extractor Registry、跨 schema 历史迁移留作后续 production hardening。

## Skill schema、版本及可信激活

`SkillDefinition` 有 skill_id、version、status、scope、applies_when、investigation_goals、evidence_strategy、safety_invariants、known_patterns、anti_patterns、allowed_guidance、created_at。

`SkillEvidenceStrategy` 明确 goal、recommended claim types、priority、rationale code。例如 ESTABLISH_PAYMENT_FINALITY / PAYMENT_FINALITY + PAYMENT_TRANSACTION_ID / SAFETY_CRITICAL / MONEY_TRUTH_FIRST。没有节点、边或 Step1/Step2 工具序列。Tool 的可用性仍由当前 ToolCapabilityCatalog 决定。

只允许先添加 DRAFT。SQLSkillRepository 的可信管理方法 `activate(skill_id, version)`、`retire(...)` 改变状态并追加 skill/version/status/time 审计；原定义 payload 保持 DRAFT 的不可变内容。相同 ID/version 不能覆盖；新内容需要新版本。Planner 只获取 ACTIVE；RETIRED 不能再次激活，需审阅新版本。

SkillComposer 稳定排序并保留各自 advisory strategy，不做后加载覆盖。General safety invariants 与 overlay 取并集。任何声明不执行安全 invariant 的 Skill 都拒绝激活或组合。某个 guidance 损坏、缺版本或冲突时，整个可选 Guidance 回退为 None，基础 Policy 继续工作。激活入口没有交给 Agent，未实现完整 IAM。

## Retrieval、相似度与适用性

`VerifiedExperienceRetriever.retrieve(snapshot, signature, top_k=3)`：

- tenant-scoped Case admission；只读取同 tenant ACTIVE 经验，不读取当前 Case 自身。
- 根据 applicable_since / applicable_until、已知协议与结构化 partner/product scope 过滤。
- 未知当前协议不猜版本；已知协议不匹配则排除。
- known 症状相同才贡献离散 overlap 分数；UNKNOWN 不算匹配。Timeout/schema flag 权重 3，其他匹配字段 1。
- 按 retrieval_score 降序、experience_id 升序，最多 3 条。

这是相似度排序，**不是**资金概率或 Hypothesis confidence。首版使用 SQL tenant/index + Python 确定性投影，无 Vector DB、Embedding、RAG 平台。内存中处理同 tenant ACTIVE 集合适用于当前 POC，跨大量历史的分页/候选召回优化未宣称完成。

## Guidance Bundle、Trust 和 Planner 接入

`InvestigationGuidanceBundle` 独立于 ReasoningContextSnapshot，包含 active_skills、verified_experiences、skill_versions、experience_ids、guidance_fingerprint，以及 Runtime 私有的 tenant/case/snapshot 绑定。

```text
当前 ReasoningContextSnapshot ─┐
                             ├─ ModelInputRenderer → PlannerDraft
独立 Guidance Bundle ─────────┘                      ↓
                                  Validator → HardPolicy → Ranking
                                                      ↓
                              Step 6 Runtime revalidation / CAS
```

Renderer 保留原三个当前状态分区；额外输出：

| 分区 | Trust | 内容 |
|---|---|---|
| organizational_guidance | TRUSTED_ORGANIZATIONAL_GUIDANCE | ACTIVE Skill 的有界策略和全部安全约束 |
| historical_guidance | VERIFIED_HISTORICAL_GUIDANCE | Capsules；明确 HISTORICAL GUIDANCE / NOT CURRENT CASE EVIDENCE |

历史 Experience 不进入 current_facts、Hypothesis、PaymentIdentity 或 Evaluator 输入。静态 System Contract 明确历史不能满足当前 Gap、确认当前假设、改变身份、授权修复、关闭 Case 或覆盖当前矛盾 Evidence。Prompt-like 错误码仍位于历史 data 分区，未采用关键词 blacklist。

`PlannerService(..., guidance_provider=service)` 支持现有 Runtime 每轮可选获取 Guidance；也接受可信调用方显式传入有 seal 和 snapshot 绑定的 Bundle。模型不能填写这个入口。错误/过期绑定的 Bundle 被忽略。基础调查、授权和关闭策略都不接受 guidance 参数。

Model input hash 包含实际渲染 Guidance；PlannerDecision 和 PlannerAuditRecord 增加 guidance_fingerprint、skill_refs、experience_refs。输入 schema 升为 2；历史 decisions 的新字段有空默认值，旧记录仍能读取。Guidance provenance 与 Evidence provenance 分开。

模型引用“历史上都查 Payment”不会产生权限。Safety omission gate、scope、budget、重复查询保护、CandidateValidator 和执行前 RuntimeActionRevalidator 保持原样。Remediation Planner、Authorization、Recovery、Evaluator 不接入 Memory；共享 Provider metadata 拆到无 Guidance 依赖的小模块，避免引入依赖循环。

## Context Budget 和 PII

当前 Snapshot 保持原预算和 seal。Guidance 另有独立的 **12,000 序列化字符、最多 4 个 Skill、合计 8 个 strategy、Top 3 Experience** 限额；每个 Capsule 最多 24 个历史 Tool、4 个错误码。它是额外显式预算，不冒充原 Snapshot 的 token usage。

超限时先减 Capsule，再减 advisory strategies；**不删除保留 Skill 的安全 invariant**。如果安全内容本身超预算，回退无 Guidance，基础硬策略仍生效。

Capsule 不包含历史 source_case_id、订单、Evidence/Observation 原文、customer/account/beneficiary token、message/transaction/callback IDs、ToolQuery、Approval、Capability、actor 或日志。原始外部标识不可能成为当前 Tool Query address。相似度不按客户匹配。持久 Experience 也仅保留来源 Case、safe order hash、哈希 Evidence refs 和类型化投影，不保存 Raw PII、Raw Callback、签名、reason_summary 或 CoT。所有 Demo 身份均为 synthetic fixtures。

## Aggregation 与改进提案

ExperiencePatternAggregator 提供可选 exact signature 分组的样本数、outcome counts、`first_evidence_yield` Tool counts、每 Tool 调用数/有新 Evidence 的调用数/新增 Evidence 数/median position。这只是首次产生新 Evidence，不等于已证明其业务效用。

`propose(skill, now)` 只产生 `SkillImprovementProposal(status=PROPOSED)`，含 base_version、证据优先级建议、支持经验 IDs、sample_size 和生成时间。至少两个独立经验中出现早期 Evidence yield 才建议该 claim；不会把同一 Case 的重复查询伪装成多样本，也不降低已有 SAFETY_CRITICAL strategy。无自动写 Skill、无自动 activate、无 LLM self-modification。

计数不是 Benchmark，更不是“效率提高 40%”。样本偏差、收益评估、真实模型冷启动对比和 Recall@K 等指标须留给独立 Benchmark；System Registry、UI-1、Money Movement 未开始。

## 实际离线 Demo

先配置仅用于本地 synthetic side effect 的 `CAPABILITY_SIGNING_SECRET`，然后运行：

```powershell
.\.venv\Scripts\python scripts/demo_memory.py --output .local/memory-demo.json
```

完整真实输出见 [organizational-memory.json](examples/organizational-memory.json)。演示使用两个独立 CLOSED_VERIFIED Case，经正常 Step 8 synthetic replay 和 Step 10 关闭后显式发布；当前 Case C 只有 TRACE 时检索它们。

```text
VERIFIED EXPERIENCES: 2
HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE
ACTIVE SKILL: tri-party-disbursement v1
COLD FAKE: ESCALATE
GUIDED FAKE: CALL get_payment_transaction
CURRENT CASE C PAYMENT EVIDENCE: NOT_EXECUTED
CURRENT H4: SUPPORTED (not CONFIRMED)
CURRENT EVALUATOR: PASS / NO_DISBURSEMENT_PATH
CURRENT CASE: INVESTIGATING (not automatically closed or published)
```

Cold / Guided 是刻意可检查的 Fake 对比 fixture，不是模型质量实验结果。当前 Payment 必须经现有 Step 6 revalidation + CAS + 当前 CaseToolExecutor 获取。后续其余 Read 是 Demo 显式调用。最终 Evaluator 完全不读取历史 Guidance。

## 验收结果

以下是 Step 11 的历史验收快照，示例 JSON 也保留原版本。Step 12 已将 Experience 新写入 schema 升为 v2：`observed_evidence_types` 替代原字段名，v1 的序列化与内容哈希保持兼容；Planner Decision/Audit 增加 GuidanceBuildStatus。新的对照实验和全量验收见 [Benchmark 文档](benchmark.md)。

本阶段新增 65 个测试实例，总计 979 个；以下为冻结功能代码的实际结果：

| 测试 | 结果 | 耗时 |
|---|---|---|
| SQLite Step 11 定向 | 65 passed | 63.31s |
| PostgreSQL Step 11 定向 | 65 passed | 94.84s |
| SQLite full suite | 976 passed, 3 skipped | 621.29s |
| PostgreSQL full suite | 977 passed, 2 skipped | 744.87s |

定向命令：`python -m pytest tests/test_organizational_memory.py -q`；PostgreSQL 配置专用测试库 `TEST_POSTGRES_URL` 后加 `--postgres`。全量命令：`python -m pytest -q` 与 `python -m pytest --postgres -q`。实际运行使用独立 `--basetemp` 并禁用 pytest cache；PostgreSQL 每个测试使用独立 schema。

测试覆盖发布资格、伪造绑定、哈希篡改、版本不可变、撤销、并发幂等、租户边界、PII 投影、Prompt injection、当前事实优先、Scope/Policy 再校验、冷启动、故障回退和 terminal approval hardening。两个 Publisher 并发只写入一条经验，在 SQLite / PostgreSQL 上均通过。

两库均跳过两个可选在线 LLM 测试；SQLite 额外跳过一个 PostgreSQL 专用测试。本次未调用在线模型。各套测试仅有一个既有 Starlette/AnyIO BlockingPortal 弃用提示。此前 Planner / Provider / Agent Runtime 兼容测试也通过：145 passed, 1 skipped。
