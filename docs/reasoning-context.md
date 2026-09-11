# Step 4 — Reasoning Context Snapshot

本阶段的边界是 `Case + Evidence → Immutable Snapshot`。没有 LLM、Prompt、Planner、Agent Loop、Next Best Action、Write Tool、Repair、embedding、Vector DB 或聊天记忆。Step 5 才消费这一结构化边界；当前 Demo 只做人工查询与确定性计算。

## 为什么独立于 diagnostic view

CaseEvidenceView 包含完整观测与全部证据，HypothesisGraphView 包含完整 Relations 和诊断信息；它们都不能直接作为模型输入。证据会增长，工具原文可能很大，历史状态不是当前事实，身份未知与安全缺口不能在文本摘要中丢失。

ReasoningContextAssembler.build(case, evidence) 只接受 Case 和 list/tuple[Evidence]。构造器只接收冻结的 ContextBudget 与 ContextEligibilityPolicy 配置；build 不接受外部 Graph、previous_context 或 previous_summary。每轮由 EvidenceIndex 校验 Case/order/tool scope、重验模型，再由 HypothesisEngine 重算身份、假设与缺口。没有数据库、Tool Client、网络或缓存依赖。

```text
durable Case + Evidence
  → EvidenceIndex / HypothesisEngine / Payment Identity
  → Information Eligibility
  → Mandatory Safety Selection
  → Relevance / deterministic Compaction
  → whole-snapshot Size Budget
  → Invariant Validation
  → ReasoningContextSnapshot
---------------- LLM boundary ----------------
  → future Planner（本阶段未实现）
```

原则是 Correctness > Eligibility > Safety > Freshness > Relevance > Compactness。Eligibility 和 Safety 无法同时满足时拒绝生成 Snapshot，不能降低证明标准。

## Schema 与版本

模型均继承 extra=forbid、frozen=True，集合使用 tuple，Snapshot 无任意 JSON/dict payload 槽。

| 部分 | 主要内容 |
|---|---|
| 标识与 provenance | snapshot_id、case/order ID、Case/Evidence/Graph 指纹、hypothesis_input_fingerprint、policy_fingerprint |
| 版本 | context schema 1、eligibility 1、compaction 1、context policy 1、hypothesis rule 3 |
| task | Goal、Success Criteria、Stop/Escalation Conditions、Forbidden Outcomes |
| financial_subject | 预期金额分、币种、客户/收款主体/账户 opaque refs；旧 Case 可为 null |
| financial_identity | MATCH/MISMATCH/UNKNOWN、具体维度、交易引用与完整身份 Evidence refs |
| current_facts | FactCapsule：claim/value/subject/business_time/observed_at/freshness/completeness/source/protocol/ref |
| hypotheses | active capsules 与精简 eliminated summaries；决定性 refs 不截断 |
| gaps | 类型化 question/required claims/priority/status/related hypotheses/refs |
| controls | 静态 Safety Invariants、Case forbidden actions、工具预算与生命周期可调查标记 |
| available_tools | Case scope 内静态只读能力说明 |
| history / omission | 查询与历史摘要；selected refs；按原因计数与 ID digest |
| context_budget_usage | 完整紧凑 JSON 字符数、近似 token、各 capsule 数量及预算上限 |

snapshot_id 是完整内容（去掉 snapshot_id 本身）的 SHA-256。Case scope 的集合排序，Evidence 按 ID 去重排序，所有选择与压缩顺序确定。相同 Case/Evidence/预算/策略得到完全相同 JSON。changed budget、denied claims、规则或策略版本会改变对应指纹与 Snapshot ID。

assembled_at 是 Evidence 最大 observed_at 的逻辑水位；无 Evidence 时用 Case.created_at，不调用 wall clock。它不是实际执行审计时间。独立 Demo 每次创建新的 Case/Simulation，Case 时间和观测 ID 不同，跨次 Demo 的 ID 不要求相同；用保存的同一 inputs 重算必须一致。

## Information Eligibility 与 PII

分类区分 BUSINESS、TOKENIZED_IDENTITY、MASKED_PII、RAW_PII、INTERNAL_CONTROL、ORACLE。当前只允许 BUSINESS / TOKENIZED_IDENTITY / INTERNAL_CONTROL；对应 PII-A/B，C/D 均禁止，即使预算充足也不能开放。没有 Capability 授权入口。

ContextEligibilityPolicy 使用显式 Claim allowlist 和正向结构化值契约。身份引用要求 CUS/BEN/ACC 格式，状态与业务语义使用现有 Enum vocabulary，当前 schema error 只允许已定义的 code/field，协议只传提取后的字段类型与语义。未知的新 Claim 或不符合结构化契约的值默认不可进入 Context。这不是对任意文本寻找手机号的 PII Scanner。

Snapshot schema 不包含 SyntheticIdentityRecord、SecretStr、PII 原始值 DTO、Observation、WorldState 或 GroundTruth。静态测试扫描 context 与 hypotheses imports；禁止 vault、Simulator、repository、数据库和 LLM SDK。纯输入边界保留原有身份引用验证、Oracle 隔离与 scope 校验。

INTERNAL_CONTROL 也不是任意控制数据透传：只投影可信 Case 创建者提供的 TaskContract、forbidden actions、预算，以及固定规则/catalog 的解释。不给模型 tenant、simulation、上游 credential、dispatch correlation、原始 Observation URI 或 Tool payload。Task 中“禁止使用模拟器答案”等禁令是控制文本，不是 Oracle 事实。

未来 incident comment、error detail、日志文本、合作方响应与合同正文必须先经过独立 Content Eligibility/Sanitization Pipeline。本版不提供 include_raw_text 接口，也不把当前可信 TaskContract 当成任意不可信长文本输入入口。真正接入用户可自由填写的任务文本时，也必须在 Case 创建边界执行该 Pipeline；本阶段没有伪造一套 DLP 或声称能过滤任意文本中的 PII。

Graph 从全部合法 durable Evidence 计算。若其 mandatory identity/confirmed witness 或关键当前字段依赖被禁止信息，抛 ContextEligibilityError；不通过删除输入重新计算一个更方便的结论。可选 capsule 含不合格引用时整项不输出，禁止输出缺证据的片面解释。

## Current 与 History

current_facts 严格复用 EvidenceIndex.current()：CURRENT、COMPLETE、source_as_of 存在，按事实维度取业务时间最新记录，并受同 scope 新 lookup failure 的失效规则约束。equal-time 互斥值不会随机选赢家。

原来观察到 Payment SETTLED，后来同 scope 新 Timeout：当前支付 Capsule 消失，Payment Identity 重新 UNKNOWN，支付终态和身份 Safety Gap 重新 OPEN。旧 SETTLED 可以留在 HistoricalStateGroup 中，明确为历史，不删原 Evidence。

历史按 claim/subject/protocol 分组，保留首末观察值、业务时间、最新 freshness、数量、首末 refs 与 range digest，不生成任何业务结论。只显示首末值不意味着中间没有其他状态；完整时间序列仍在 Evidence Store。

## Mandatory Tier 0

永不因大小压缩丢弃：完整 Task / Forbidden Outcomes、FinancialSubject、结构化 Payment Identity、Safety Contract、全部 SAFETY_CRITICAL OPEN Gap、全部 CONFIRMED Hypothesis、决定性证明引用及其当前事实。

关键当前 Claim 包括 REQUEST_SENT、HTTP_RESPONSE_STATUS、FUND_BUSINESS_STATUS、GUARANTEE_STATUS、CALLBACK_GATEWAY_RECEIVED、MESSAGE_CONSUME_STATUS，以及支付 finality / transaction / request / amount / currency / customer / beneficiary / account。

Identity MISMATCH 和 UNKNOWN 保留枚举与具体维度；不得写成 false 或“大体匹配”。例如收款主体不符时，SETTLED 和金额可以出现，但 MISMATCH/BENEFICIARY 及 PAYMENT_IDENTITY OPEN 也必须保留。保护的是关键**当前**事实；STALE 历史不是当前支付依据。

Tier 1 为 supported hypotheses、discriminating gaps、直接支持事实；Tier 2 为 possible/unknown、其他相关当前事实；Tier 3 为精简 eliminated、lookup/history。安全 gap 相关的 possible/unknown 先于其他同层候选。当前十个假设默认都保留状态；预算不足时仅保留 Tier 0 和按序可容纳的可选项。

Protocol relevance 当前限定 loanNo 字段与 SUCCESS 语义，或实际支持引用涉及的字段。完整协议、无关字段与非活动命题历史无需每轮展开。

## 确定性 Compaction

Repeated lookup 按 tool + query scope（含协议版本/effective_at）+ lookup status 分组：count、first/last observed_at、最新 freshness/completeness、first_ref、latest_ref、range_digest。不把 1,000 个 ID 复制进 Snapshot。

Hypothesis supporting/contradicting refs 中的重复 lookup 同样压缩到首末，另保留原引用数量与 digest，避免从另一路径重新带回全部 ID。决定性 refs 始终完整保留。十次 Callback NOT_FOUND 仍然只是查询缺失，不能改写为从未收到。

ELIMINATED 保存 hypothesis ID、statement、status、decisive refs，不展开全部 Relations。History Digest 只描述次数、时间和观察值，不写“综合看来已经放款”。

## Budget 与 Fail Closed

ContextBudget 默认：60,000 serialized chars / 64 facts / 16 hypotheses（含 resolved summaries）/ 24 gaps / 20 history items。大小按 `snapshot.model_dump_json()` 的完整紧凑 JSON 测量，包含选中 refs、omission audit、所有版本与预算字段；不是只计 facts 的长度。导出文件结尾换行是文件 framing，不属于 JSON 内容。

预算字段本身也占字符，通过确定性长度收敛后封存内容 hash。字符数/3 向上取整仅是固定近似，不宣称等于任何 GPT/Claude tokenizer。改变序列化格式时必须重新测量，不能把 pretty JSON 长度当成相同预算。

首先测 Tier 0；若任一上限不足，抛 MandatoryContextOverflow，不返回残缺 Snapshot。随后按分层顺序逐项尝试可选 capsule，超限则保留 omitted 原因；审计元数据导致最终包变大时，从最低优先级可选项回退，绝不截断字符串或关键 refs。若连 mandatory audit envelope 都无法容纳，同样拒绝。未来 Runtime 可换更大窗口或升级人工，本版不作该决策。

## Provenance、Selected 与 Omitted

selected_evidence_refs 是所有输出结构实际引用的闭包，必须属于输入。摘要首末引用也算 selected；中间已压缩的引用算 omitted，其事实次数由 digest 说明。

OmittedEvidenceSummary 为每一条未选 Evidence 记录唯一原因组：ELIGIBILITY_DENIED、REPEATED_LOOKUP、HISTORICAL_SUPERSEDED、IRRELEVANT_TO_ACTIVE_HYPOTHESES、SIZE_BUDGET。组保存 count 和 evidence_ids_digest，不复制全量 ID。选中数 + 各 omitted 数必须等于去重后总输入数。

指纹与审计 inputs 可以重放回答“当时尚无该证据”“eligibility 禁止”“budget 没放下”。`.inputs.json` 是可信审计附件，不是模型输入；只含 Case/Evidence，完整底层 Tool provenance 仍在已有数据库。本阶段不新增 Context 数据库、缓存、记忆或 Event Sourcing Framework。

ReasoningContextInvariantValidator 检查 input/graph 指纹、模型类型、Tier 0 引用与关键 FactCapsule、完整安全 Gap、身份结果、Task/Safety Contract、确认 witness、selected 引用闭包、omission 计数和完整 JSON 大小。即使关键 Evidence ID 仍在 Hypothesis 中，删除关键 FactCapsule 也被拒绝。

## Tool Capability ≠ Tool Selection

ToolCapabilityCatalog 静态描述全部 12 个现有只读 Tool：名称、描述、produces claim types、READ_ONLY 风险、数据分类、成本与延迟档位。档位仅是当前静态估计，不是测量 SLA，也不用于行动排名。

Snapshot 仅显示 Case.allowed_tools 与 catalog 的交集，并排除明确 forbidden action 对应的 Tool 名；列表按名称排列，不推荐调用顺序。预算/生命周期还保留 investigation_allowed 标记，真正 dispatch 仍由 CaseToolExecutor 再校验。没有 PAYMENT 权限时不显示该能力，PAYMENT_FINALITY Gap 仍可 OPEN。Assembler 不替 Planner 选择工具或决定升级人工。

## Demo 与实际输出

```powershell
.\.venv\Scripts\python scripts/demo_reasoning_context.py --scenario S6 --output .local/s6-context.json
.\.venv\Scripts\python scripts/demo_reasoning_context.py --scenario S8 --output .local/s8-context.json
```

脚本通过现有 Harness HTTP 路由完成手工调查，之后只把 Case/Evidence 交给 Assembler。导出 `.json`、`.preview.txt` 与 `.inputs.json`。Human Preview 不是 Snapshot 真相。Scenario 只存在可信外层 setup，不进入 Context。

| 实际样例 | Evidence 输入/选中 | 当前 Facts | History items | 字符数 | Identity |
|---|---:|---:|---:|---:|---|
| [S6 Snapshot](examples/s6-reasoning-context.json) | 30 / 22 | 22 | 0 | 29,735 | MATCH |
| [S8 Snapshot](examples/s8-reasoning-context.json) | 6 / 4 | 0 | 2 | 13,725 | UNKNOWN |
| [500 条压力测试](examples/context-stress-result.json) | 500 / 26 | 22 | 2 | 31,250 | MATCH |

S6 保留 H4/H6/H6_SCHEMA CONFIRMED，H6_STALE/H8 SUPPORTED；安全 gap 为 FUND_PROTOCOL_APPLICABILITY，调查 gap 包括 DEPLOYED_CONSUMER_SCHEMA_VERSION、ASSET_CONVERGENCE。H6_STALE 的支持规则已额外要求父级同 Callback 的 Gateway/FAILED Evidence witness，rule version 为 3。

S8 保留十个 POSSIBLE，无确认或排除；Payment Identity UNKNOWN；PAYMENT_FINALITY/PAYMENT_IDENTITY 均为 SAFETY_CRITICAL OPEN。三次 Fund NOT_FOUND 与三次 Payment TIMEOUT 压缩成两个 group；不产生支付 FAILED 命题。

压力输入含 300 条重复 lookup、100 条旧状态、70 条无关协议字段及 30 条当前调查 Evidence。仅选 26 个 refs；298 条中间 lookup、98 条中间历史和 78 条非相关事实按原因计数，关键当前 facts、确认见证、安全 gap 与身份结果保留。测试检查结构性质和乱序重放一致，不硬编码必须选中某个条数。

## 回归验证

本次新增 **38 个测试实例**，包括 H6_STALE 父级 witness 负向测试、类型/静态依赖隔离、完整 JSON 字符预算、mandatory overflow、身份 MISMATCH/UNKNOWN、关键事实删除检测、新 Timeout、历史及重复查询压缩、500 条压力输入、确定性重算与真实 Harness 路由 Demo。

- SQLite 全量：**226 passed，1 skipped，39.14s**。跳过的是 PostgreSQL 专用测试。
- PostgreSQL 全量：**227 passed，74.50s**。
- 两套各有 2 条现有 FastAPI/Starlette 测试客户端依赖弃用警告，无失败。
- S6/S8 的 Context 与 Graph 示例均已用各自保存的审计输入重放，JSON 逐字段一致。

## 留给 Step 5

不接 OpenAI/Claude/Gemini，不写 Agent Prompt、不实现 Planner、Agent Loop、Tool Selection/Ranking、Repair 或 Write Tool。本版也不实现自然语言 sanitization、真实 PII/IAM/Capability、向量库、embedding、聊天 Summary 或长期记忆。下一阶段必须消费经过此边界的 Snapshot，不能改为直接序列化 diagnostic view。
