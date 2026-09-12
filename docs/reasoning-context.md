# Step 4.2 — Full Context Envelope Eligibility / Model Trust Boundary

Step 13 增量：合法 durable resume 设置可信 `Case.lookup_retry_after`，装配为 `HistoryDigest.retry_window_start`。保留全部历史事实和引用，仅对当前重试窗口计算连续失败次数；Invariant Validator 核对该时间与 Case 输入一致。Context schema/policy 当前为 5，compaction 为 4，eligibility 仍为 4；历史版本记录保留。详见 [Durable Orchestration](orchestration.md)。

本组件的边界是 `Case + Evidence → Immutable Snapshot`，只做确定性计算。Step 5 的 [Planner](planner.md) 消费此边界；独立 [Step 6 Runtime](agent-runtime.md) 在受控只读查询前后重新装配 Snapshot。Context 本身没有执行或持久化依赖，Context Demo 继续只做人工查询与确定性投影。下文分阶段记录保留历史基线；当前 Graph aggregate ruleset 为 4，见 Agent Runtime 的 Gap 入口补全说明。

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
  → Step 5 Planner（验证、过滤、排序后 STOP）
```

原则是 Correctness > Eligibility > Safety > Freshness > Relevance > Compactness。Eligibility 和 Safety 无法同时满足时拒绝生成 Snapshot，不能降低证明标准。

## Schema 与版本

模型均继承 extra=forbid、frozen=True，集合使用 tuple，Snapshot 无任意 JSON/dict payload 槽。

| 部分 | 主要内容 |
|---|---|
| 标识与 provenance | snapshot_id、case/order ID、Case/Evidence/Graph 指纹、hypothesis_input_fingerprint、policy_fingerprint |
| 版本 | context schema 4、eligibility 3、compaction 3、context policy 4、hypothesis rule 3 |
| task | Goal、Success Criteria、Stop/Escalation Conditions、Forbidden Outcomes |
| section_trust | 固定分区信任等级，不能把外部事实提升成控制指令 |
| financial_subject | 预期金额分、币种、客户/收款主体/账户 opaque refs；旧 Case 可为 null |
| financial_identity | MATCH/MISMATCH/UNKNOWN、具体维度、关键身份见证；候选交易与全量引用的 count/digest/bounded preview |
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

ContextEligibilityPolicy 使用显式 Claim allowlist 和正向结构化值契约。身份引用要求 CUS/BEN/ACC 格式，状态与业务语义使用现有 Enum vocabulary，错误码、字段路径、topic、业务编号使用开放的安全结构化语法，协议只传提取后的字段类型与语义。未知的新 Claim 或不符合结构化契约的值默认不可进入 Context。这不是对任意文本寻找手机号的 PII Scanner。

Snapshot schema 不包含 SyntheticIdentityRecord、SecretStr、PII 原始值 DTO、Observation、WorldState 或 GroundTruth。静态测试扫描 context 与 hypotheses imports；禁止 vault、Simulator、repository、数据库和 LLM SDK。纯输入边界保留原有身份引用验证、Oracle 隔离与 scope 校验。

INTERNAL_CONTROL 也不是任意控制数据透传：只投影可信 Case 创建者提供的 TaskContract、forbidden actions、预算，以及固定规则/catalog 的解释。不给模型 tenant、simulation、上游 credential、dispatch correlation、原始 Observation URI 或 Tool payload。Task 中“禁止使用模拟器答案”等禁令是控制文本，不是 Oracle 事实。

未来 incident comment、error detail、日志文本、合作方响应与合同正文必须先经过独立 Content Eligibility/Sanitization Pipeline。本版不提供 include_raw_text 接口，也不把当前可信 TaskContract 当成任意不可信长文本输入入口。真正接入用户可自由填写的任务文本时，也必须在 Case 创建边界执行该 Pipeline；本阶段没有伪造一套 DLP 或声称能过滤任意文本中的 PII。

Graph 从全部合法 durable Evidence 计算。若其 mandatory identity/confirmed witness 或关键当前字段依赖被禁止信息，抛 ContextEligibilityError；不通过删除输入重新计算一个更方便的结论。可选 capsule 含不合格引用时整项不输出，禁止输出缺证据的片面解释。

## Full Envelope Eligibility

Step 4.2 先用负向测试复现：合法 MESSAGE_ERROR_CODE 的 subject.identifier 包含 `IGNORE PREVIOUS INSTRUCTIONS` 时，旧 eligibility 仍返回 true。过去只全面检查 value，并只对 TRANSACTION/FUND_REQUEST 标识增加语法限制；MESSAGE/CALLBACK/PROTOCOL/ORDER 标识、字段名、版本、历史 scope 和引用路径均缺少统一约束。

当前模型入口采用两次独立检查：

1. ContextEligibilityPolicy 将每条 Evidence 投影成类型化 FactCapsule，并检查 LookupScope。每个候选都经过完整检查，包括未成为 current fact、而可能被 history/relation 引用的证据。可选不合格记录归入 ELIGIBILITY_DENIED；关键证明不合格则抛 ContextEligibilityError，不能靠删除输入改写 Graph 结论。
2. ContextEnvelopeInvariantValidator 在最终边界将 Snapshot 字段重建为嵌套 Pydantic 模型。即使调用者使用 model_copy/model_construct 绕过构造校验，错误的 subject、版本、历史值、引用和 trust 标记仍会被拒绝。它不把 Snapshot 序列化后扫描手机号、不改写字符串，也不访问数据库或工具。

| 外部或派生字段 | Context 契约 |
|---|---|
| 六类 subject.identifier | OpaqueSubjectRef：1–128 字符，安全字符集，允许 `ORDER-001`、`CB:20260911:001`、`MSG_001`、`SIM-FUND@2.3`；禁止空白、控制字符、裸个人号码形态 |
| FUND_REQUEST / TRANSACTION identifier | 同时维持 OpaqueBusinessRef 的金融业务引用契约，无合作方前缀要求 |
| Case case_id / internal_order_id、FactSubject.internal_order_id | OpaqueSubjectRef；仅增加 Context 投影检查，不修改 Case domain 的历史兼容契约 |
| PROTOCOL_FIELD_TYPE 的 subject.field | 必须存在，使用 StructuredFieldPath，可包含 `repaymentPlan.items[0].dueDate` |
| PROTOCOL_BUSINESS_SEMANTICS 的 subject.field | 必须存在，使用 StructuredErrorCode 风格的 uppercase 状态键；不能拿字段路径冒充业务状态 |
| 其他非空 subject.field | StructuredFieldPath |
| protocol_version / source_version / Callback 版本值 | 共用 StructuredVersion：1–64 字符，有限分段语法；支持 `2.3`、`v2.3`、`2026.09`、`release-20260911`，兼容短整数 revision |
| LookupScope | 与 Fact 共用安全 order/version 类型；原 ToolQuery 的合作方协议格式规则仍独立存在 |
| Evidence refs、preview refs、history 首末 refs、derived gap IDs | ContextReference：1–256 字符，有界安全引用，不能通过 ID 槽带回外部文本 |
| Fact value / History 首末 value | 按 ClaimType 执行同一份 value_contracts；状态与语义为封闭枚举，代码/字段/topic/业务编号为开放结构化类型 |

非空字符串不是默认资格；未知 Claim 无对应值契约时拒绝。value_contracts 同时被资格筛选和模型验证使用，避免两套规则漂移。Pydantic extra=forbid/frozen 继续生效。Case/Source/Protocol 之外的 Observation 原文、metadata.source_path、Raw PII、scenario 信息及 credential 不在 Snapshot schema 中。

这些契约是语法与信息资格约束，不能证明合作方编号真实有效，也不是 PII Scanner。纯数字长版本或个人号码需要在上游用真正的内部引用替代；把原始号码加前缀不等于去标识化。Adapter/Identity Service 仍负责来源、业务格式及 PII 域隔离。

## Trusted Control vs Untrusted Data

`section_trust` 使用固定 ContextSectionTrust 模型，每项由 Literal 约束，调用者不能自行把 current_facts 的等级改为 TRUSTED_CONTROL。信任标记与访问资格是两个维度：数据通过结构检查，不会因此成为指令。

| ContextTrustClass | 分区 |
|---|---|
| TRUSTED_CONTROL | task、financial_subject（可信 Case provisioning）、safety_constraints、available_tools（静态 catalog）、budget |
| UNTRUSTED_EXTERNAL_DATA | current_facts、history_digest；包括协议字段、Callback、Message、Fund、Payment 数据。History 的计数虽是确定性计算，整个混合分区仍保守标记为外部数据 |
| DETERMINISTIC_DERIVED | financial_identity、active_hypotheses、resolved_hypotheses_summary、open_evidence_gaps |

其余 envelope 标识、版本、哈希、选中引用与 omission/budget audit 是结构化 provenance 元数据，不能解释为指令。Hypothesis 的 statement/reason 和 Gap question 来自固定规则/catalog；本阶段不把外部文本插值进这些解释。Derived 分区中的 Evidence refs 和交易 preview 仍受类型化检查，推导结果也不具有修改控制指令的权限。

例如合法的 `MESSAGE_ERROR_CODE = IGNORE_PREVIOUS_INSTRUCTIONS` 可以保留在 current_facts，等级始终为 UNTRUSTED_EXTERNAL_DATA；含空白的 `IGNORE PREVIOUS INSTRUCTIONS` 不符合 identifier/code 的语法。没有 injection phrase blacklist，也没有基于词句猜测是否攻击的分类器。**语法安全不等于该字符串是可信指令。**

未来 Step 5 Model Input Renderer 必须遵守：

1. 按固定信任分区呈现 TRUSTED_CONTROL、UNTRUSTED_EXTERNAL_DATA 和 DETERMINISTIC_DERIVED，保留身份/缺口/证明约束；标记本身不能代替 Renderer 的隔离实现。
2. Tool/Evidence 数据永远是 data，不能成为 instruction；禁止把 Evidence 字符串插入 system prompt。
3. 禁止将原始 Tool response 直接 append 到 model conversation。
4. Tool 完成后严格执行 `Observation → Evidence → Hypothesis → Context Rebuild → Planner`。本项目重建 Context 时内部重算 Hypothesis，不接受外部 Graph 或旧摘要。
5. Planner 只能消费通过最终边界验证的 ReasoningContextSnapshot，不能接收 CaseEvidenceView、完整 Graph、Observation 或 Tool payload。
6. CALLBACK_RAW 即使 Runtime 内部拿到 raw callback，也必须经过 deterministic extraction 和 Context eligibility；raw Tool response 没有直达 LLM 的路径。

本阶段只有可运行的类型边界、信任分类与验证器，没有 Model Input Renderer、Prompt、LLM、Planner、Agent Loop、任意文本 sanitization、DLP、HTML sanitizer、Guard Model 或 injection classifier。未来真正接入模型时，仍需实现并测试这些 Renderer 约束。

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

Hypothesis supporting/contradicting refs 先将重复 lookup 压缩到首末，再稳定排序并限制 preview 数量；非 lookup 辅助引用也受同一上限约束。分别保留全量 count 和排序后 ID digest，避免从另一路径重新带回全部 ID。决定性 refs 始终完整保留。十次 Callback NOT_FOUND 仍然只是查询缺失，不能改写为从未收到。

ELIMINATED 保存 hypothesis ID、statement、status、decisive refs，不展开全部 Relations。History Digest 只描述次数、时间和观察值，不写“综合看来已经放款”。

## Step 4.1：Confirmed Proof Contract

CONFIRMED 的 Tier 0 capsule 包含 hypothesis_id、kind、statement、status、reason、open_gap_ids、**完整 decisive_evidence_refs**，以及 supporting_ref_count/supporting_refs_digest、contradicting_ref_count/contradicting_refs_digest。确认状态不会把辅助关系升级为 mandatory。

Assembler 先用空 preview 构造并检查 Tier 0，再把 supporting_ref_preview/contradicting_ref_preview 作为可撤回的 Tier 1 增量。它们不额外占一个 hypothesis capsule；预算不够时撤回 preview，完整 decisive witness 不受影响。Invariant Validator 对每个 CONFIRMED capsule 检查 decisive refs 与 Graph 精确相等，并验证辅助 count/digest、preview 子集及上限。

辅助关系不能通过 optional FactCapsule 或历史再次无界展开。只有有界 preview 或独立 mandatory contract 需要的事实才保留；Graph 继续保存所有关系。500 辅助引用测试使用未来规则的 synthetic graph producer，明确给一个已确认命题 8 个 decisive 和 500 个 supporting refs；不修改生产 Graph schema 或规则。

## Step 4.1：Closed schema / Open Structured Vocabulary

| 类型 | 安全结构契约 | 示例 |
|---|---|---|
| OpaqueBusinessRef | 严格字符串，1–128 字符，字母、数字、`- _ : . /`；禁止裸个人号码形态 | `20260911SSBANK000812`、`TXN:HAIER:20260911:001` |
| StructuredErrorCode | `[A-Z][A-Z0-9_]{1,63}` | `SIGNATURE_INVALID`、`PROTOCOL_VERSION_UNSUPPORTED` |
| StructuredFieldPath | 最多 128 字符，有限标识符、点路径和至多六位数字下标 | `repaymentPlan.items[0].dueDate` |
| StructuredTopic | 最多 128 字符，有限字符组成的点分段 | `partner.loan.callback.dlq` |

这些类型只扩展现有 Claim 的安全 vocabulary，不开放任意 Claim、payload 或自由文本槽。换行、控制字符、空白、HTML/Markdown payload、超长值仍被拒绝；状态、币种和业务语义继续使用 Enum。字段资格通过后，仍要接受 relevance 与预算选择，并不意味着一定出现在当前 Snapshot。

Loan/request/transaction 不再绑定 PAY/FREQ/LN。是否符合具体合作方的编号规范，属于 Tool / Adapter / Extraction Boundary，Context 只检查结构安全。裸全数字和身份证末位 X 形态不能当作 opaque ref；合法但纯数字的合作方编号需由 Adapter 使用真正的内部替代引用。给原始 PII 添加前缀不等于 tokenization；当前语法验证不是 PII Scanner，也不能证明输入已去标识化。个人 CUS/BEN/ACC refs 保留已有类型域，真实 token 生成与敏感数据管理仍属于 Identity Service。

Transaction preview 同样验证 OpaqueBusinessRef，不能借 full Identity Result 绕过资格边界。关键事实若资格不符仍 fail closed，不能删除它们重新计算更方便的安全结论。

## Step 4.1：Identity Result 与 Context Projection

PaymentIdentityResult 保留完整候选 witness 和全部 Evidence refs，验证规则和 Graph 不变。纯 IdentityContextProjector 输出原样的 result、mismatch_dimensions、unknown_dimensions、verification_version，额外提供 candidate_transaction_count、transaction_ref_preview、transaction_refs_digest、evidence_ref_count、evidence_refs_digest。digest 覆盖完整排序后的集合，preview 不是完整候选清单。

- MATCH：稳定选择一个产生 MATCH 的完整单 observation witness，保留全部八个交易维度和请求关联见证。
- MISMATCH：保留**每一个**直接导致 mismatch dimensions 的事实，以及对应交易/请求/终态关联；不只抽样一条错误记录。
- UNKNOWN：多个交易时保留稳定排序后的两个完整不同交易 witness，足以证明唯一交易关联未建立。每个缺失维度保留一个实际受影响 observation 的关联上下文；同一交易存在等时互斥值时额外保留两种实际冲突值。无观测时保留明确 unknown dimensions 和安全 Gap，不虚构事实。

候选的其余 matching auxiliary refs 由 count/digest 表示，不能再经 current_facts、relation preview 或历史全部展开。Mandatory current facts 中的支付字段针对关键 identity witness 投影；其他请求、资金、Callback/消费事实仍按原政策保留。若某个候选字段同时是 Graph 的 decisive witness 或安全 Gap witness，则独立证明契约优先，仍完整保留。

100 个全匹配候选仍为 UNKNOWN/TRANSACTION，不挑一个方便的交易改成 MATCH。投影只保留两组完整见证、最多 3 个交易 preview；即使第 100 个候选收款主体不符，MISMATCH/BENEFICIARY 及其具体错误证据也必须保留。辅助候选从 10 增至 100 不会导致 Context 线性增长。

这里平衡安全与可用性：允许压缩辅助信息以避免无意义 overflow，但不声称任何规模都能装下。若 100 个候选各有独立真实 mismatch、或 Graph 本身要求数百条 decisive refs，关键证明仍可能超过窗口，此时抛 MandatoryContextOverflow。预算压缩不能把 MISMATCH 变 UNKNOWN，或把 UNKNOWN 变 MATCH。

## Budget 与 Fail Closed

ContextBudget 默认：60,000 serialized chars / 64 facts / 16 hypotheses（含 resolved summaries）/ 24 gaps / 20 history items；每个 relation preview 默认最多 4 条，每个 identity transaction preview 默认最多 3 条，二者可配置为 0，硬上限均为 32。大小按 `snapshot.model_dump_json()` 的完整紧凑 JSON 测量，包含选中 refs、omission audit、所有版本与预算字段；不是只计 facts 的长度。导出文件结尾换行是文件 framing，不属于 JSON 内容。

预算字段本身也占字符，通过确定性长度收敛后封存内容 hash。字符数/3 向上取整仅是固定近似，不宣称等于任何 GPT/Claude tokenizer。改变序列化格式时必须重新测量，不能把 pretty JSON 长度当成相同预算。

首先测 Tier 0；若任一上限不足，抛 MandatoryContextOverflow，不返回残缺 Snapshot。随后按分层顺序逐项尝试可选 capsule，超限则保留 omitted 原因；审计元数据导致最终包变大时，从最低优先级可选项回退，绝不截断字符串或关键 refs。若连 mandatory audit envelope 都无法容纳，同样拒绝。未来 Runtime 可换更大窗口或升级人工，本版不作该决策。

## Provenance、Selected 与 Omitted

selected_evidence_refs 是所有输出结构实际引用的闭包，必须属于输入。摘要首末引用也算 selected；中间已压缩的引用算 omitted，其事实次数由 digest 说明。

OmittedEvidenceSummary 为每一条未选 Evidence 记录唯一原因组：ELIGIBILITY_DENIED、REPEATED_LOOKUP、HISTORICAL_SUPERSEDED、IRRELEVANT_TO_ACTIVE_HYPOTHESES、SIZE_BUDGET、RELATION_COMPACTED、IDENTITY_COMPACTED。后两项表示策略主动压缩，不能冒充预算不足或无关。重复 lookup 仍优先标注 REPEATED_LOOKUP，资格不符仍优先标注 ELIGIBILITY_DENIED。组保存 count 和 evidence_ids_digest，不复制全量 ID。选中数 + 各 omitted 数必须等于去重后总输入数。

指纹与审计 inputs 可以重放回答“当时尚无该证据”“eligibility 禁止”“budget 没放下”。`.inputs.json` 是可信审计附件，不是模型输入；只含 Case/Evidence，完整底层 Tool provenance 仍在已有数据库。本阶段不新增 Context 数据库、缓存、记忆或 Event Sourcing Framework。

ReasoningContextInvariantValidator 检查 input/graph 指纹、模型类型、Tier 0 引用与关键 FactCapsule、完整安全 Gap、身份结果、Task/Safety Contract、确认 witness、selected 引用闭包、omission 计数和完整 JSON 大小。即使关键 Evidence ID 仍在 Hypothesis 中，删除关键 FactCapsule 也被拒绝。

## Tool Capability ≠ Tool Selection

ToolCapabilityCatalog 静态描述全部 12 个现有只读 Tool：名称、描述、produces claim types、contributes_requirements、READ_ONLY 风险、数据分类、成本与延迟档位。档位是静态估计，不是测量 SLA；Context 不做选择，Step 5 在硬过滤后用于确定性排名。GUARANTEE 可贡献资金请求协议适用性，TRACE/FUND/PAYMENT/GUARANTEE 可贡献请求关联，CALLBACK/MESSAGES 可贡献事件关联；无 Tool 声称可取得实际部署 schema 版本。

Snapshot 仅显示 Case.allowed_tools 与 catalog 的交集，并排除明确 forbidden action 对应的 Tool 名；列表按名称排列，不推荐调用顺序。预算/生命周期还保留 investigation_allowed 标记，真正 dispatch 仍由 CaseToolExecutor 再校验。没有 PAYMENT 权限时不显示该能力，PAYMENT_FINALITY Gap 仍可 OPEN。Assembler 不替 Planner 选择工具或决定升级人工。

## Demo 与实际输出

```powershell
.\.venv\Scripts\python scripts/demo_reasoning_context.py --scenario S6 --output .local/s6-context.json
.\.venv\Scripts\python scripts/demo_reasoning_context.py --scenario S8 --output .local/s8-context.json
```

脚本通过现有 Harness HTTP 路由完成手工调查，之后只把 Case/Evidence 交给 Assembler。导出 `.json`、`.preview.txt` 与 `.inputs.json`。Human Preview 不是 Snapshot 真相。Scenario 只存在可信外层 setup，不进入 Context。

| 实际样例 | Evidence 输入/选中 | 当前 Facts | History items | 字符数 | Identity |
|---|---:|---:|---:|---:|---|
| [S6 Snapshot，schema 4](examples/s6-reasoning-context.json) | 30 / 20 | 20 | 0 | 29,614 | MATCH |
| [S8 Snapshot，schema 4](examples/s8-reasoning-context.json) | 6 / 4 | 0 | 2 | 16,077 | UNKNOWN |
| [500 条压力测试](examples/context-stress-result.json) | 500 / 24 | 20 | 2 | 30,532 | MATCH |

S6 保留 H4/H6/H6_SCHEMA CONFIRMED，H6_STALE/H8 SUPPORTED；安全 gap 为 FUND_PROTOCOL_APPLICABILITY，调查 gap 包括 DEPLOYED_CONSUMER_SCHEMA_VERSION、ASSET_CONVERGENCE。H6_STALE 的支持规则已额外要求父级同 Callback 的 Gateway/FAILED Evidence witness，rule version 为 3。

S8 保留十个 POSSIBLE，无确认或排除；Payment Identity UNKNOWN；PAYMENT_FINALITY/PAYMENT_IDENTITY 均为 SAFETY_CRITICAL OPEN。三次 Fund NOT_FOUND 与三次 Payment TIMEOUT 压缩成两个 group；不产生支付 FAILED 命题。

压力输入含 300 条重复 lookup、100 条旧状态、70 条无关协议字段及 30 条当前调查 Evidence。仅选 24 个 refs；298 条中间 lookup、98 条中间历史、78 条非相关事实与 2 条辅助关系按原因计数，关键当前 facts、确认见证、安全 gap 与身份结果保留。测试检查结构性质和乱序重放一致，不硬编码必须选中某个条数。

以下压力表和 [候选交易压力输出](examples/context-hardening-result.json) 保留 Step 4.2 的 schema 3 基线，由保存的 S6 Evidence 构造 synthetic 候选，重放反序 Evidence 的 Snapshot 完全一致；这些是投影层压力 fixture，并非额外调用 Tool 获得的新观测。对应性质测试在 Step 5 schema 4 上仍全量执行。

| 候选交易 | Identity | 全量引用 | Context 关键引用 | preview | 当前 Facts | 完整 JSON 字符 |
|---:|---|---:|---:|---:|---:|---:|
| 10 | UNKNOWN / TRANSACTION | 82 | 18 | 3 | 18 | 23,816 |
| 100 | UNKNOWN / TRANSACTION | 802 | 18 | 3 | 18 | 23,822 |
| 100，第 100 个 beneficiary 不符 | MISMATCH / BENEFICIARY | 802 | 22 | 3 | 22 | 26,230 |

100 个候选中其余 784 条辅助引用被标记 IDENTITY_COMPACTED；含收款主体错误时保留该错误维度及关联见证。500 supporting 测试在 30,000 字符预算下成功；8 decisive refs 全保留，支持和反对 preview 各最多 4 条。真正超大的 decisive 证明仍抛 MandatoryContextOverflow。

## 回归验证

Step 4.2 新增 **93 个测试实例**，Step 0–4.1 的 300 个既有测试实例全部保留。先复现旧策略允许恶意 Message subject 的漏洞，再验证 subject 六种 kind、协议字段/状态键、版本、Case 标识、历史 scope/首末值、Evidence 引用、绕过构造器后的最终验证，以及不可提升的 trust 标记。结构化 `IGNORE_PREVIOUS_INSTRUCTIONS` 作为外部错误码仍可保留；CALLBACK_RAW 的真实 Harness 路由回归验证仅提取后的数据进入 Snapshot。

- SQLite 全量：**392 passed，1 skipped，75.29s**。跳过的是 PostgreSQL 专用测试。
- PostgreSQL 全量：**393 passed，129.57s**。
- 两套各有 2 条现有 FastAPI/Starlette 测试客户端依赖弃用警告，无失败。
- S6/S8 Context 示例已用原有审计输入重建为 schema 3，并验证乱序重放一致。eligibility/context policy 为 3；compaction 保持 2。
- 未修改 Case、Simulator、HypothesisGraph 或 PaymentIdentityResult 的 domain contract。既有 dispatch correlation、Case scope、PII 隔离、500 supporting 和 100 candidate 压力测试继续通过。
- `git diff --check` 通过。本阶段未实现 Step 5。

## Step 5 接入补充

上文回归数字和压力样例记录 Step 4.2 的历史基线。Step 5 新增只读 capability requirement 描述、RepeatedLookupGroup.latest_consecutive_count、HistoryDigest.lookup_history_complete，因此 schema/context policy 升至 4、compaction 升至 3。连续计数考虑相同 Tool/scope 中间的成功观测；预算/资格省略历史，或 Evidence 去重导致 Observation 来源不足以覆盖 Case.used_tool_calls 时，Planner 保守禁止 CALL_TOOL。既有累计 range/count 及事实、证明和隔离语义保留。

ModelInputRenderer 按 section_trust 输出独立分区，外部引用映射稳定 alias，私有反向映射不传模型。diagnostic view 仍不能作为 Planner 输入。详细错误语义、运行命令、测试与 Step 6 的停止边界见 [Planner 文档](planner.md)。Context 本身没有 SDK、Prompt、Provider 或执行器依赖。

Step 11 的 Skill / Experience 保存在独立 `InvestigationGuidanceBundle`，不写入本 Snapshot、不改变本 Context 的三种 trust class 或 Evidence/Graph 指纹。历史 Guidance 有独立预算与 provenance，由 Planner Renderer 添加到另外两个分区；它不能满足当前 Gap 或确认 Hypothesis。详见 [Organizational Memory](organizational-memory.md)。
