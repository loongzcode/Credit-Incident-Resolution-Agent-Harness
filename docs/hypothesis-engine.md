# Step 3.1：Hypothesis Integrity、Financial Identity 与 PII Boundary

唯一入口是 `HypothesisEngine.evaluate(case, evidence)`。这是 Case 与 Evidence 的确定性投影，不接 LLM、Prompt、Planner、Agent Loop、Tool Ranking、Context Assembly、写工具、Repair、Capability 或 Evaluator；不新增 case.root_cause 或结案入口。

## 边界与目录

```text
Reality → Observation → Evidence → Relation → Hypothesis State → Evidence Gap

src/credit_harness/hypotheses/
  models.py    # Definition / State / Relation / Gap / Graph 和 Enum
  catalog.py   # 10 个静态定义、父子关系、规则 ID 和 ruleset version
  index.py     # 只读 EvidenceIndex、质量/时间过滤和请求关联
  rules.py     # 确定性确认、消除与证据组合
  gaps.py      # 缺少哪些事实，不指定工具
  engine.py    # 无状态重算、引用汇总、输入指纹
  invariants.py # 拒绝非法父子状态，不传播状态
src/credit_harness/identity/
  models.py    # FinancialSubject / Verification Result / 未来 PII Access Intent
  service.py   # 可替换的引用校验接口与精确比较实现
  payment.py   # 同交易、同观测的完整支付身份契约
  vault.py     # 不对外导出的 synthetic Raw PII 边界
scripts/demo_hypothesis_graph.py
tests/test_hypotheses.py
docs/examples/s6-hypothesis-graph.json
docs/examples/s6-hypothesis-graph.evidence.json
docs/examples/s8-hypothesis-graph.json
docs/examples/s8-hypothesis-graph.evidence.json
```

Engine 没有构造依赖，不接收 client、repository、previous state 或自定义 Relation。evaluate 只接受 Case 和 list/tuple[Evidence]，拒绝 Observation、WorldState、GroundTruth、任意字典，以及跨 Case、主订单或工具 scope 的 Evidence。它不访问 Simulator、数据库、原始 Observation、文件或网络。测试检查类型入口、静态 imports，并在工具/原文读取被禁止时执行计算。

调用方应使用可信 EvidenceRepository 提供输入。Engine 无法在不读取 Observation 的前提下重新认证来源，也不声称隔离能任意构造 Python 对象的恶意代码。Graph 可以导出为审计文件，本阶段不新增 Hypothesis/Relation 数据库表或写 API。Demo 同时保存实际 Evidence 输入 sidecar；底层 Observation 和 dispatch 关联仍由原有数据库保留。

## Evidence 与 Hypothesis

Evidence 是直接观察到的字段，Hypothesis 是对它们的精确解释。HTTP TIMEOUT 不能证明未放款；完整 PaymentIdentityWitness MATCH，加上 HTTP TIMEOUT，可以确认 H4 的状态模式，但不能定位具体网络原因。

Definition 静态保存 kind、statement、description、confirmation/elimination rule ID、relevant claim types 和 parent ID，不含 Case Evidence。State 保存支持、反对、决定性 Evidence IDs、尚缺 Gap IDs、reason、rule_version 和 evaluated_at。

CAUSAL 表示原因候选，STATE 表示现实状态模式，SEMANTIC_GUARD 表示字段证明能力的边界。H4 是 STATE，H8 是 SEMANTIC_GUARD。父子关系通过 Definition.parent_hypothesis_id 表示，不自动传播状态，也不把所有节点称为事故根因。

## 状态与为什么不用概率

| 状态 | 含义 |
|---|---|
| UNKNOWN | 没有足够证据或调查中上下文；不是 false |
| POSSIBLE | Case 处于 INVESTIGATING，候选合理但没有合格直接支持 |
| SUPPORTED | 有直接支持，确认条件尚不完整 |
| CONFIRMED | 版本化 confirmation rule 的决定性见证全部成立 |
| ELIMINATED | 决定性证据足以否定该精确定义命题 |

状态表达证明条件，不表达统计概率。没有 confidence、百分比、投票或重复查询计分。没有证据与存在反证保持区别。UNKNOWN/STALE/PARTIAL 不会因为重复出现而升级成确定事实。

每次从完整输入重算，不读取先前状态；证据撤回、后续失败、规则升级均可改变结果。没有 `previous_status + new_evidence` 作为唯一真相，也没有聊天记忆。确认 Hypothesis 不是最终资金验收、写权限或结案授权。

`HYPOTHESIS_RULESET_VERSION = "2"`，State 保存该版本，Relation 保存 `.v2` rule_id。`input_fingerprint` 对规范化 Case、按 ID 排序去重的完整 Evidence 和版本计算 SHA-256。输入乱序或重复相同 Evidence，输出不变；同 ID 内容互异则拒绝。

`evaluated_at` 是**逻辑水位**：最大 Evidence.observed_at，转 UTC；无证据时用 Case.created_at。它不是实际机器运行时间。这个设计使同一输入连时间字段也可完全重现；未来实际执行审计时间应由外层另记。

## Index、Freshness 与关联

EvidenceIndex 提供 all、current、latest、has_value、refs，返回完整 Evidence，保留 subject、freshness、completeness、event_time 等 provenance；内部使用 tuple 与只读映射。

current 要求 CURRENT、COMPLETE、source_as_of 存在。latest 按完整 subject、字段与协议版本取最大业务时间；同一时刻的不同值保留，不任意选赢者。STALE 历史不删除，相关引用可留作 CONTEXT_ONLY。相同工具和查询范围更新或同时刻的查询失败，会阻止旧观测继续充当当前证据。

v2 决定性规则使用明确工具来源的 PRIMARY 记录，不根据 strength 推断业务事实。即使标记 AUTHORITATIVE，不完整证据也不能决定状态。本阶段没有复杂 TTL 或 source lineage 推理；原 Conflict Detector 保持不变。

为避免 Hypothesis 层重新解析 Observation，提取器版本 **3** 延续版本 2 的直接可见关联 Metadata，并新增三个 Payment 身份引用 Claim。S6 现在有 **30 条 Evidence**：

| Metadata | 直接来源 | 用途 |
|---|---|---|
| fund_request_id | Trace / Fund / Payment / Guarantee / LoanNote DTO 同名字段 | 原请求识别与跨系统关联 |
| callback_event_id | MessageRecord.event_id | 消息关联 Gateway 中同一个事件 |

旧 Evidence 的新增字段默认为 null，不回填猜测值；H1 等单字段判断仍可使用旧记录，但缺少链接时不确认 H4/H6。重新查询生成版本 3 Evidence，版本参与原有 fingerprint，不覆盖历史。Metadata 不复制原始返回，不读取隐藏状态。

原请求优先用当前 PRIMARY Trace/Guarantee 的显式链接；没有锚点时可使用唯一 PRIMARY Fund 请求记录。Payment 不能用自己的 request 字段自证它就是本订单的正确请求。仅调用 Payment 时，终态可已知，但 REQUEST 身份维度仍 UNKNOWN。H4 额外要求 Trace 自己的请求链接；完整条件见 Business Identity Binding。

H6 要求同 Callback event 的 Gateway 与消息记录，并检查事件时间先后。H6_SCHEMA 要求失败状态、错误码、loanNo、expected/actual 类型来自同一个 message subject、同次 Observation、同一事件时间。

## v2 确认与消除规则

| 假设 | 确认条件 | 消除或保守限制 |
|---|---|---|
| H1 REQUEST_NOT_SENT | 完整当前 Trace 的 REQUEST_SENT=false | true 消除；不完整/冲突不作决定 |
| H2 FUND_NOT_ACCEPTED | 本版延后确认 | 同请求可靠业务记录或 Payment Identity MATCH 消除；NOT_FOUND 只作上下文 |
| H3 FUND_PROCESSING_FAILED | 本版延后确认 | Fund FAILED 支持；Payment Identity MATCH 消除；不能从 HTTP 结果推断 |
| H4 PAYMENT_SETTLED_WITHOUT_SUCCESSFUL_HTTP_RESPONSE | 同请求 TIMEOUT + Payment Identity MATCH | HTTP OK 否定其传输前提；不定位网络原因 |
| H5 CALLBACK_NOT_OBSERVED_AT_GATEWAY | 不从当前查询契约确认全局缺失 | NOT_FOUND 最多支持；Gateway 收到则消除 |
| H6 CALLBACK_CONSUMPTION_FAILED | 同 Callback 已到达 + 消费 FAILED | 已消费成功且无仍未解决的关联失败时消除 |
| H6_SCHEMA_MISMATCH | 父级关联 Gateway/FAILED witness + 同一失败记录的错误码、loanNo 和互异类型 | 缺任一项不确认；后续消费成功后不维持子级 CONFIRMED，原始错误 Evidence 仍保留 |
| H6_STALE_CONSUMER_SCHEMA | 本版禁止确认 | 类型与关联 Callback/历史协议相符只支持；缺部署事实 |
| H7 ASSET_NOTIFICATION_FAILED | Guarantee SUCCESS + delivery FAILED + Asset PROCESSING | 前两项仅支持；Asset SUCCESS 消除当前未收敛 |
| H8 FUND_SUCCESS_NOT_EQUAL_PAYMENT_FINALITY | 原请求协议绑定 + 唯一匹配文档中的 SUCCESS separate-payment 语义 | 单有协议文档仅支持；Payment SETTLED 不消除语义守卫 |

H2/H3/H6_STALE 的 confirmation rule ID 明确标为 confirmation_deferred。缺少数据源契约时不为了 Demo 完整强行确认。后续扩展需要新的直接证据与规则版本，不接受调用者传入确认标签。

### 协议适用性与 H6_STALE

CALLBACK_PROTOCOL_VERSION=2.3 只证明 Callback 的协议，可用于比较回调字段类型，不能证明整个资金请求使用 2.3。

S6 七次调查没有 Guarantee Evidence，因此 H8 为 **SUPPORTED** 并留下 FUND_PROTOCOL_APPLICABILITY Gap。若额外取得 Guarantee 当前 Evidence，可用其 protocol_version 和 fund_request_id Metadata 绑定原请求，再匹配唯一协议文档；同版本有多个 partner 时仍视为歧义，不能只挑符合假设的文档。当前查询范围限定本订单；未来多 partner 业务需要明确 partner/request 契约。

H6_SCHEMA 能确认，因为同 Callback 的父级 Gateway/FAILED witness 成立，且消息系统直接报告字段与互异类型。H6_STALE 不能确认，因为“期待 integer”与“2.2 定义 integer”不能证明“部署版本就是 2.2”。必须保留 DEPLOYED_CONSUMER_SCHEMA_VERSION 的 OPEN Gap，不能用自由文本 error detail 补出部署结论。

## Evidence Relation 与 Evidence Gap

Relation 仅由规则生成：SUPPORTS / CONTRADICTS / DECISIVE_SUPPORT / DECISIVE_CONTRADICTION / CONTEXT_ONLY，每条有 Case、Hypothesis、Evidence IDs、reason 和 rule_id。没有接收调用者“E 支持 H”并写入的 API。

Gap 描述缺什么事实，不含 next_tool。required_claim_types 为已有 ClaimType 或尚未采集的 UncollectedClaimType；后者只是需求，不能冒充 Evidence。priority_class 为 SAFETY_CRITICAL / DISCRIMINATING / SUPPORTING，不是工具排序。

Gap 状态为 OPEN / SATISFIED / UNAVAILABLE。本版没有结构化的不可用证明，因此不会因预算、scope 或查询失败猜出 UNAVAILABLE；该枚举保留给将来的来源可用性契约。

Fund SUCCESS 不满足支付终态 Gap；当前明确 Payment SETTLED/NOT_EXECUTED 才满足字段需求，PENDING 仍未决。身份关联另有 SAFETY_CRITICAL Gap，满足支付字段不代表最终资金验收。后续查询失败重新打开当前证明缺口，不删除历史支付事实。

Gateway 已到而无消费状态时生成消费缺口；取得关联 FAILED 后满足该缺口，并评估技术错误事实。schema mismatch 字段齐备后技术缺口满足，但部署版本缺口继续 OPEN。

S5 连续十次 Callback NOT_FOUND 仍不能确认从未发送。Gateway Gap 要求区分未发送、未到达、到达但索引不可见；重复缺失不是否定事实。

## 多假设与 Demo

多个节点描述不同层次，不做 winner-takes-all。S6 同时确认 H4、H6、H6_SCHEMA_MISMATCH，分别刻画传输/支付模式、消费状态及直接技术触发条件，不强选唯一事故原因。

```powershell
.\.venv\Scripts\python scripts/demo_hypothesis_graph.py --output .local/s6-graph.json
.\.venv\Scripts\python scripts/demo_hypothesis_graph.py --scenario S8 --output .local/s8-graph.json
```

脚本先通过已有 CaseToolExecutor 和 HTTP Tool 路由进行人工调查，再只把 Case 与 Evidence 交给 Engine。Scenario 只用于外层可信 setup。脚本打印每个状态的完整 supporting/contradicting/decisive Evidence IDs 和 Open Gap，并导出 Graph 与 `.evidence.json` 输入，不打印隐藏答案。

实际 [S6 Graph](examples/s6-hypothesis-graph.json)：H1/H2/H3/H5 ELIMINATED；H4/H6/H6_SCHEMA_MISMATCH CONFIRMED；H6_STALE_CONSUMER_SCHEMA/H8 SUPPORTED；H7 POSSIBLE。Open Gap：DEPLOYED_CONSUMER_SCHEMA_VERSION、FUND_PROTOCOL_APPLICABILITY、ASSET_CONVERGENCE。输入见 [S6 Evidence](examples/s6-hypothesis-graph.evidence.json)。

实际 [S8 Graph](examples/s8-hypothesis-graph.json)：重复 Fund/Payment 三轮后，十个节点均为 POSSIBLE，无 Confirmed/Eliminated；PAYMENT_FINALITY 与 PAYMENT_IDENTITY 均为 SAFETY_CRITICAL OPEN Gap，不产生支付失败结论。输入见 [S8 Evidence](examples/s8-hypothesis-graph.evidence.json)。

## Parent / Child Integrity

H6_SCHEMA_MISMATCH 是 H6 的 specialization。子级确认规则直接组合父级的 Gateway received + 同 Callback event 的 consume FAILED，以及该失败消息的错误码、loanNo 和 expected/actual 类型。错误 Evidence 还要求同 message subject、同 observation_id、同 event_time、同 callback_event_id 和 content_hash。

只有消息错误、没有关联 Gateway 时，子级最多 SUPPORTED。没有 `child confirmed -> parent.status = confirmed` 的状态传播。Engine 返回前执行只读 HypothesisGraphInvariantValidator，遍历全部有 parent_hypothesis_id 的节点；若 child CONFIRMED 而 parent 非 CONFIRMED，抛 HypothesisInvariantError，不修复 Graph。重复定义、缺失父级、循环层级也被拒绝。child ELIMINATED 不影响 parent。

## Business Identity Binding

**存在 SETTLED Transaction ≠ 当前订单正确完成放款。** FinancialSubject 是 Case 的结构化任务输入，保存 expected_principal_minor、currency、customer_ref、expected_beneficiary_ref、expected_account_ref。当前 S6 的已知输入为 2,000,000 分 CNY / CUS-JD-001 / BEN-JD-001 / ACC-JD-001。不从自然语言 goal 解析，也不把调查后才能获得的 fund_request_id 填进 Case。

旧 Case 可以没有 financial_subject；旧 Payment 可缺少三个引用，结果为 UNKNOWN，不自动填入 S6 fixture。Case JSON payload 已能持久化新增对象，不新增数据库表或回填猜测值。

完整 PaymentIdentityWitness 要求八项同时满足：

1. PAYMENT_FINALITY = SETTLED；
2. PAYMENT_TRANSACTION_ID 等于 transaction subject 的 ID；
3. TRANSACTION_FUND_REQUEST_ID 等于当前请求锚点；
4. PAYMENT_AMOUNT 等于 Case.expected_principal_minor；
5. PAYMENT_CURRENCY 等于 Case.currency；
6. PAYMENT_CUSTOMER_REF 等于 Case.customer_ref；
7. PAYMENT_BENEFICIARY_REF 等于 Case.expected_beneficiary_ref；
8. PAYMENT_ACCOUNT_REF 等于 Case.expected_account_ref。

八项均需来自 PRIMARY Payment 的当前完整 Evidence，同一 transaction subject、同一次 Observation、同一 event_time，并具有一致的 content_hash/source_as_of/observed_at。版本 3 的 Payment finality 从存在的 transaction.payment_finality 提取并使用 transaction subject；无交易时保留请求级 finality，但它不能成为完整身份 witness。

IdentityVerificationService 接口只定义客户、收款主体、账户核对；当前实现严格比较引用，不看姓名、不做模糊匹配、不访问 Vault、不注册 Agent Tool。verify_payment_identity 只读取 Case 与索引后的 Evidence，结果是 MATCH / MISMATCH / UNKNOWN，记录 mismatch_dimensions、unknown_dimensions、每个候选 witness 及 Evidence refs。Graph 保存该独立 verification result；它不是 Hypothesis，也不写回 Evidence Store。

缺字段或当前证据互斥返回 UNKNOWN；直接观测到字段不一致返回 MISMATCH，并可同时报告其他缺失维度。多个交易不会任选一笔匹配交易作为订单付款证明，需后续 Reconciliation。分散在多个 Observation 或交易的零散字段不能拼成 MATCH。没有比例、评分或“多数匹配”。

H4 仅在原请求 HTTP TIMEOUT + 完整身份 MATCH 时 CONFIRMED。H2/H3 也只接受 MATCH 支付作为 decisive elimination。H2 仍可被独立的同请求 Fund 业务记录排除；金额错误的 Payment 不会替代那条证据。

PAYMENT_FINALITY Gap 描述来源是否直接观察到明确终态；PAYMENT_IDENTITY Gap 是 SAFETY_CRITICAL，描述是否完整匹配本 Case。SETTLED + 错误收款主体时，前者 SATISFIED、后者 OPEN、identity=MISMATCH。仅有 Payment、没有独立请求锚点时，identity=UNKNOWN。REQUEST_ASSOCIATION 只回答请求关联，不替代金额或账户校验。

## PII and Identity Boundary

**所有身份数据完全为 synthetic test fixtures。** 按企业金融数据边界设计，但没有连接真实客户、银行、加密或身份服务，也不使用真实姓名、证件、银行卡或手机号。

```text
Raw PII（内部 synthetic vault，TEST-*）
  ↓ 未来 Tokenization / Customer Identity / Payment Account Service
Opaque Internal Refs（当前 CUS-* / BEN-* / ACC-*）
  ↓ 严格相等的 deterministic identity verification
Verification Result（MATCH / MISMATCH / UNKNOWN + Evidence refs）
  ↓
Evidence-based Agent Reasoning（尚未实现 Context Assembly）
```

四层边界：

| 层 | 本阶段设计 | 暴露范围 |
|---|---|---|
| Raw PII | identity/vault.py 的 SyntheticIdentityRecord；只接受 TEST-*，使用 SecretStr 避免 repr/JSON 直接输出值 | 仅可信内部 fixture 容器；无 reveal API |
| Token / Reference | FinancialSubject、PaymentTransactionRecord、三个身份 Claim | Case/tenant/order/tool 授权范围内的 PII-B |
| Verification | 精确引用比较及 PaymentIdentityResult | 可进入未来合规的 reasoning context |
| Masked PII | 未来人工调查/Approval UI 经权限判断展示 | 本阶段无 UI、无 masking/reveal endpoint |

Raw record 不被 API、Extractor 或 Hypothesis 导入。Payment Tool 使用独立 allowlist DTO，显式投影字段，不能序列化整个服务端 transaction。测试在服务端交易子类加入 synthetic Raw PII，验证 Tool projection 不输出。Evidence 的身份 claim 校验引用格式；Engine 重验输入模型，拒绝通过 model_copy 绕过校验的原始身份值。

三个引用不是不可识别的匿名数据，仍是敏感的关联标识；格式限制不是加密或 DLP。SecretStr 也不是加密。当前边界防止设计内的数据域混用，不能宣称能清洗任何任意自由文本中的 PII。未来协议正文、错误文本等数据源扩展时，必须先定义字段 eligibility 和投影，不能直接交给模型。

未来权限层级：PII-A 普通业务字段；PII-B tokenized reference；PII-C masked PII；PII-D Raw PII。未来 Agent 默认只访问 A/B；人工特殊调查通过审批访问 C；D 默认不进入 LLM Context。Tool credential 和 CaseToolExecutor 的现有权限、预算、dispatch provenance 保持原有边界。

Purpose Binding 必须回答“谁、为什么、哪个 Case、哪个主体、哪些字段、多久”。PIIAccessIntent 预留 case_id / purpose / subject_ref / allowed_fields / expires_at / actor / audit_id，严格冻结且拒绝额外字段；它仅是未来策略输入，构造该对象不代表授权，不校验或发放 Capability。真实访问以后必须 purpose-bound、time-bound、field-scoped、audited。本版不实现审批、运行时 TTL 授权、审计账本或权限决策器。

Step 4 Reasoning Context Snapshot 的 Information Eligibility：允许任务必需的 customer_ref、beneficiary_ref、account_ref、identity_match_result、Evidence IDs 与 Business Facts；masked display 仅在权限允许时。默认禁止完整证件号、银行卡号、手机号、原始身份材料和无关合同 PII。Context Engineering 不只是 Token Compression；本阶段只规定边界，不开始 Context Assembly。

Production Roadmap 才包括 HSM、KMS、银行 tokenization、PCI DSS 系统、完整 RBAC/ABAC/IAM、Vault、OAuth、DLP 和实际访问审计。当前不实现上述能力，不把身份结果当作写权限、最终验收或结案证明。

## Step 3.1 回归结果

新增 30 个测试实例，SQLite 全量 **188 passed / 1 skipped，35.44s**；PostgreSQL 全量 **189 passed，67.72s**。SQLite 跳过 PostgreSQL 专用测试。两套均有 2 条已有 FastAPI/Starlette 测试客户端依赖弃用警告，无失败。

Wrong Amount / Currency / Customer / Beneficiary / Account / Request 六类负向样例均经真实 Simulator → HTTP Tool → Observation Persistence → Evidence Store 路径验证为 MISMATCH；H4 不确认，且不因该 Payment 消除 H2/H3。跨 Transaction、跨 Observation 和缺账户字段返回 UNKNOWN。独立 Fund 受理 Evidence 仍可消除 H2，不与支付身份结果混淆。

另验证父级 witness 包含关系、非法 Graph 拒绝且不修改、错误消息/事件关联、PII 引用校验、模型校验绕过拒绝、Vault synthetic-only、未来服务端私有字段投影隔离、Harness 各读取/Tool 路由及 OpenAPI 无 Raw PII。

按本次契约更新原测试两项确认前提：schema 确认先取得关联 Gateway；Payment 用于消除假设先取得独立请求锚点。保留消息-only、Payment-only 的新负向测试。S6 数量断言从 27 更新为 30，规则版本断言从 1 更新为 2。

本次实际 Demo：S6 为 30 Evidence / 58 Relation / identity MATCH / 3 Open Gap；S8 为 6 Evidence / 12 Relation / identity UNKNOWN / 4 Open Gap。两份导出 Graph 均用其 sidecar 输入重新计算，逐字段完全一致。全量回归继续覆盖原 Simulator、权限、预算、dispatch correlation、防 replay 与 provenance。

## Step 3 历史回归基线

Step 3 历史基线新增 41 个测试实例，保留既有 Simulator、Dispatch Correlation 与 Evidence 安全边界测试。

- SQLite 完整套件：158 passed，1 skipped，23.93s。跳过的是只针对 PostgreSQL 的持久化测试。
- PostgreSQL 完整套件（`--postgres`）：159 passed，46.52s。
- 两套均有 2 条来自 FastAPI/Starlette 测试客户端依赖的弃用警告，无失败。

覆盖 S6/S8 实际调查链、重复缺失、跨请求/Callback 错误关联、过期与不完整 Evidence、冲突值、协议适用性、确定性重算与 Oracle 隔离。

## 留给 Step 4/5

本阶段不实现 Context Assembly、Reasoning Context Snapshot、LLM、Prompt、Planner、Next Best Action、Tool Ranking、Agent Loop、Write Tool、Repair、Capability、Evaluator、最终 RCA 或结案。两个 View 都用于 diagnostic/inspection，不应直接作为未来 Planner 的完整上下文。

后续还需要 source lineage、明确的协议 partner/request 契约、部署版本证据、复杂 TTL、时间区间冲突与规则升级策略。本版不扩展通用推理或 Event Sourcing 框架。
