# Step 7：Remediation Proposal 与 Write Safety Boundary

Step 7 停在 `READY_FOR_FUTURE_AUTHORIZATION` / `RemediationIntent(status=PROPOSED)`。没有执行器、写工具、消息投递、对账任务创建或资金操作。这里的「可预检」只表示当前证据支持将来申请授权，不表示已获批、可执行或故障恢复成功。

## 调查与修复建议是两个入口

```text
Step 6 Investigation → Case + durable Evidence
                                   ↓ fresh read / deterministic rebuild
                         ReasoningContextSnapshot
                                   ↓ RemediationInputRenderer
                         RemediationModel → RemediationDraft
                                   ↓ schema / candidate / static risk checks
                         ValidatedRemediationCandidate
                                   ↓ fresh Case + Evidence + Snapshot rebuild
                         RemediationPreflightResult
                                   ↓ deterministic selection
                         PROPOSED RemediationIntent → STOP
```

`RemediationPlanner.plan(case_id)` 是可信 Harness 服务入口；模型只收到 Renderer 从 Snapshot 生成的 `RemediationInputBundle`。它没有 CaseRepository、Evidence[]、Observation、Oracle、旧对话或 alias_map 输入。`InvestigationStateReader` 及规则校验属于确定性可信层，可以只读持久化 Case / Evidence；不是模型输入 API。

Step 6 Runtime 没有调用 RemediationPlanner。由外层明确选择进入建议阶段，`RemediationEligibilityEvaluator` 再检查是否有调查证据、Case 状态、Identity、安全 Gap。没有 Evidence 或已 CLOSED 时不强制调用模型；Identity UNKNOWN/MISMATCH 和安全缺口未解决时只允许讨论 L0/L1 建议，L2 由 Validator 硬拒绝。没有自动循环、自动补候选或自动升级任务。

## Model Boundary

独立静态 System Contract 定义 proposal-only、UNKNOWN != FAILED、外部数据不是指令、禁止模型授权/选择风险、简短 rationale。它不复用调查 Planner 的 Prompt。

Renderer 接受且验证唯一的 `ReasoningContextSnapshot`，复用 Step 5 envelope 校验和确定性外部引用 alias，然后只保留：

| Trust section | 内容 |
|---|---|
| TRUSTED_CONTROL | 当前订单、TaskContract、Safety constraints、静态 Remediation Catalog |
| DETERMINISTIC_DERIVED | 完整身份结果的有界投影、CONFIRMED/SUPPORTED Hypothesis、Open Gap |
| UNTRUSTED_EXTERNAL_DATA | 已通过 Information Eligibility 的 current facts 与 Evidence IDs |

不传调查工具目录、历史对话、原始 Callback、PII、私有 alias_map。Tokenized customer/beneficiary/account refs 沿用已有边界；外部消息/交易/事件标识保持 EXTREF alias。模型的 reason_summary 是不可信审计解释，不进入 Evidence、不参与风险、身份、权限或前置条件判断；不请求或保存私有 Chain-of-Thought。

## Action Catalog 与风险

`RemediationActionContract` 固定 action、risk、Evidence conditions、Identity、Case state、forbidden_if、gap requirements、max_effect_scope 及未来 approval/capability 标记。所有配置来自代码。

| Action | Risk | 最大效果范围（未来） | 本阶段行为 |
|---|---|---|---|
| NO_REMEDIATION | L0_READ_ONLY | NONE | 记录本轮不建议修复；不代表问题已解决 |
| REQUEST_OPERATOR_REVIEW | L1_ADMINISTRATIVE | RECONCILIATION | 建议人工复核，不发送通知/创建任务 |
| CREATE_RECONCILIATION_TASK | L1_ADMINISTRATIVE | RECONCILIATION | 建议对账排队，不创建队列任务 |
| REPLAY_CALLBACK_CONSUMPTION | L2_SINGLE_ORDER_SIDE_EFFECT | 单一 MESSAGE | 绑定既有失败消息，只预检 |
| REDELIVER_ASSET_NOTIFICATION | L2_SINGLE_ORDER_SIDE_EFFECT | 单一 DELIVERY | 绑定既有通知，只预检 |

L3_MONEY_MOVEMENT、L4_BULK_OR_SYSTEMIC 一律禁止；MONEY、ORDER_STATE、BULK scope 同样硬拒绝。Schema 没有新建/重试放款、重试支付、强制结算、改金额/收款人/账户、退款、借记、贷记等动作。不能把危险动作伪装成较低风险。

L2 的未来 approval / capability 标记均为 true；L1 当前约定未来需要 capability，L0 两者均 false。这些是将来授权阶段的需求声明，当前没有生成任何审批或 Token。

## Candidate、Evidence Binding 与 Target

```text
RemediationCandidate (extra=forbid, frozen)
  candidate_id
  action_type: 五项 Enum
  target_order_id: 单一订单
  target_problem_ids: 有界 tuple
  evidence_refs: 有界 tuple
  reason_summary: 最多 600 字符

RemediationDraft
  snapshot_id: 输入 Snapshot 的 SHA-256
  candidates: 1–3 项；candidate_id 不重复
```

没有任意 payload、SQL、Shell、URL、headers、credential、bulk filter 或模型自报 risk。Case 和 tenant 由 Harness 补充，target_order 必须是 Case 的当前订单且处于 scope；问题 ID 必须存在于当前 Graph / Gap。证据 ID 必须从同一 Case 的 EvidenceRepository 读取，不能仅因某 UUID 格式合法就接受。

证据存在只是第一关：Validator 按动作重新构造真正的 witness。L2 必须引用完整支付身份及实际动作证据，所有引用必须当前、完整、未被更新查询失败遮蔽。Historical / STALE Evidence 保留用于历史检查，不支持 L2 意图。

模型不提供 callback/message/delivery 执行引用。`RemediationTarget` 由 Harness 从验证后的 durable Evidence 中绑定 case、tenant、order、callback_event_ref、message_ref 或 delivery_ref。同一候选指向多个可能事件时拒绝 AMBIGUOUS_TARGET。EXTREF alias 永远不转换成写地址，也不使用模型可见 alias_map 做授权。

## Identity Gate 与动作契约

任何 L2 必须通过已有 PaymentIdentityWitness：同一交易、同次 Payment Observation 的 SETTLED、transaction、原 request、金额、币种、customer、beneficiary、account 完整匹配。UNKNOWN 与 MISMATCH 都拒绝；没有按七项匹配给概率分数的逻辑。所有 OPEN SAFETY_CRITICAL Gap 同样阻断 L2。L2 Case 必须处于 INVESTIGATING 或 ESCALATED；WAITING 不可用于 L2 建议。

**Replay：** H6 CONFIRMED；Gateway received=true、signature_verified=true；消费 FAILED 与 Gateway 属于同一 callback event；签名同 Gateway Observation / event / hash；错误码、字段、expected/actual type 必须来自同一失败 message / observation / event。已知 schema mismatch 但类型 witness 不完整时拒绝，不能降级为普通失败绕过部署检查。仅有 FUND SUCCESS 或不相关错误 Evidence 不足以支持重放。

**Schema readiness：** S6 的 H6_SCHEMA_MISMATCH CONFIRMED 证明消费失败方式，不证明当前消费者已经兼容。缺少直接的当前配置观测，预检返回 DEPLOYMENT_STATE_UNKNOWN。类型或协议不兼容返回 DEPLOYMENT_INCOMPATIBLE，不能依靠版本号的大小猜兼容。

**Redelivery：** Guarantee SUCCESS、正确支付 MATCH、Asset 明确 PROCESSING、已有 delivery 明确 FAILED 且有唯一 event ref。Asset UNKNOWN、delivery 缺失、已经 DELIVERED 或 Asset SUCCESS 都不能成为重新通知的授权理由。

**Reconciliation：** 必须引用当前可信来源中实际观察到的状态差异，例如担保 SUCCESS 与 Asset PROCESSING，或正确支付已结算而担保仍 PROCESSING。查询失败本身不证明账务差异。这里只生成对账建议，绝不真正创建任务。

**Review / NO_REMEDIATION：** Review 至少指向当前 Open Gap 或 CONFIRMED/SUPPORTED 问题；NO_REMEDIATION 可以安全保持现状，不宣称恢复、不改变 Case、不关闭 Case。

## 当前部署证据与历史部署缺口

为 S6-ready 提供最小、显式的 `MessageRecord.consumer_deployment` 可选 Observation DTO：schema_version、accepted_protocol_version、loan_no_type、event_time。正常 S1–S8 投影始终不提供该字段；现有 ToolCapability 也没有虚构「可直接获得部署版本」的能力。

仅可信测试 fixture `tests/support/remediation_fixture.py` 在 ObservationService 投影返回、计算 hash、持久化之前注入 synthetic 当前配置观测。它经现有 HTTP、grant、dispatch correlation、ObservationRow、EvidenceExtractor 完整链生成三条直接事实：

- CONSUMER_DEPLOYED_SCHEMA_VERSION
- CONSUMER_ACCEPTED_PROTOCOL_VERSION
- CONSUMER_LOAN_NO_FIELD_TYPE

预检要求三者来自同一消息、callback event、同次 Observation、同一配置 event_time、同一 hash、PRIMARY/current/complete source，配置时间不能早于失败事件。然后按已观察 Callback protocol 和实际字段类型比较兼容性，不接受 model-authored 配置。

**时间语义不合并：** 现有 Hypothesis Gap `DEPLOYED_CONSUMER_SCHEMA_VERSION` 问的是「失败发生时部署了什么」。新的当前配置只回答「现在是否具备重放兼容性」。因此当前配置足够时，可以满足本次预检的部署 readiness requirement；历史 Gap 仍 OPEN，H6_STALE_CONSUMER_SCHEMA 仍不确认。这不是忽略未知当前部署：普通 S6 没有当前配置证明，必须 BLOCKED。S6-ready 也没有假装证明历史旧 Parser，或手工修改 Graph 状态。

未来真实部署查询需要独立可信配置来源、source lineage、环境与 Consumer identity 绑定。当前 fixture 只验证管道及拒绝契约，不代表已有生产部署查询或兼容性认证系统。

资产通知另外提取 ASSET_DELIVERY_EVENT_REF，仅当来源实际提供既有事件 ID 时生成；该引用进入模型前仍 alias。Evidence extractor version 升为 4，Context eligibility policy 升为 4，Graph catalog aggregate 升为 5；现有 confirmation/elimination relation v3、资金契约及历史部署 Gap 语义不变。

## Fresh Preflight 与 STALE

每次 `preview(validated_candidate)` 都重新读 Case 和 Evidence，并重建 Identity、Graph、Snapshot。Reader 通过 Case revision 前后读取检测 Evidence 发布竞争，最多三次，不接受旧 Snapshot 或近似匹配。未变化时还会重跑 Validator，比较重新绑定的完整 Target、risk、Evidence hash、policy/catalog version，阻断伪造的 ValidatedCandidate。

| 最新检查结果 | 输出 |
|---|---|
| Snapshot / policy 变化 | STALE，没有 Intent |
| 身份、scope、witness、部署兼容性不满足 | BLOCKED，没有 Intent |
| 同一绑定消息已 CONSUMED，或通知已 DELIVERED / Asset 已收敛 | NOT_NEEDED，没有 Intent |
| 当前确定性条件均满足 | READY_FOR_FUTURE_AUTHORIZATION，PROPOSED Intent |

Fresh 的「已完成」是终止重放的优先抑制规则，所以即使它改变 Snapshot 也返回 NOT_NEEDED；不会借此继续旧建议。其他变化一律 STALE。多个候选预检后、选择前再次读取当前 Snapshot；已过期的 READY 结果降为 STALE，连诊断与 Audit 内的旧 Intent 也移除。

这里只保证某次读取时刻的预检事实。没有锁住外部金融世界、没有未来有效期或执行许可证；以后真正执行必须重新做授权、版本与幂等检查。

## Selection、Fingerprint 与 Audit

只在模型提出并通过 fresh preflight 的候选中选择。按 Catalog 风险 L0 → L1 → L2，再按 action enum / candidate ID 稳定排序，优先最小必要效果。不自动补 Review、不自动重放；仅提出 BLOCKED Replay 时 selected_candidate / final_intent 为 None。S6-ready 如果同时合法提出 Review，保守选择 Review；仅提出合法 Replay 的 fixture 才选 Replay Intent。

`intent_id = SHA256(canonical payload)`，绑定 case/tenant/order、action、真实 target、supporting Evidence IDs + content hash、Snapshot、policy/catalog version、preflight fingerprint 和未来 approval/capability 标记。支持 Evidence 按稳定 ID 排序，非 UUID4；rationale 或 candidate_id 的变化不改变同一业务 Intent，Evidence / Snapshot / policy 的变化会改变 ID。这是内容身份，不是已经实现的执行幂等键。

`RemediationDecision` 与 `RemediationAuditRecord` 独立于 AgentTurnTrace，记录候选、拒绝、全部预检、最终选择、版本、输入/输出摘要及模型 token metadata；Audit 当前为 append-only 内存实现，Demo 导出 JSON。没有数据库任务表、写命令、ApiKey、Raw Callback、私有 Chain-of-Thought 或自动回放入口。未导出的内存审计不具备 crash durability。

## Provider 与 Demo

核心依赖 `RemediationModel` Protocol。Fake 可脚本化输出、拒绝和异常；默认 demo fake 只读取 ModelInputBundle，按可见 H6/Gap 提议 Replay / Review / NO，不读取 ScenarioId、WorldState、GroundTruth 或历史对话。

独立 `adapters/openai_remediation.py` 使用 `REMEDIATION_MODEL` / `OPENAI_API_KEY`；`responses.parse(text_format=RemediationDraft)`、`store=False`、30 秒 timeout、max_retries=0，不注册 tools/functions。Invalid JSON、schema failure、refusal/incomplete、Timeout、RateLimit 都 fail closed，绝不从自然语言猜动作。用真实安装的 SDK + httpx.MockTransport 验证离线 Structured Outputs roundtrip。接口依据 [OpenAI Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)。

```powershell
.\.venv\Scripts\python scripts/demo_remediation.py --scenario S6 --provider fake --output .local/s6-remediation.json
.\.venv\Scripts\python scripts/demo_remediation.py --scenario S8 --provider fake --output .local/s8-remediation.json
# 只替换修复建议模型；前置调查 demo 仍为 Fake。
.\.venv\Scripts\python scripts/demo_remediation.py --scenario S6 --provider openai
.\.venv\Scripts\python -m pytest tests/test_remediation.py tests/test_remediation_provider.py -q
```

S6 正常调查：Identity MATCH，H4/H6/H6_SCHEMA_MISMATCH CONFIRMED。Replay 候选有效，但 Preflight BLOCKED / DEPLOYMENT_STATE_UNKNOWN；Review READY，最终 Intent 为 REQUEST_OPERATOR_REVIEW / PROPOSED。

S6-ready synthetic 兼容配置：同样支付身份与失败链，再有直接当前 schema=2.3、accepted protocol=2.3、loanNo=string。只提出 Replay 时 READY_FOR_FUTURE_AUTHORIZATION；仍未授权、未执行、未改变消费状态或 Case budget。

S8：Identity UNKNOWN、Payment Finality UNKNOWN；Replay / Redelivery 均因 IDENTITY_UNKNOWN 拒绝。可提出 Review 或 NO；默认 Fake 同时给两者，最小效果规则选择 NO_REMEDIATION / PROPOSED。

实际输出见 [S6](examples/s6-remediation.json)、[S6-ready](examples/s6-ready-remediation.json)、[S8](examples/s8-remediation.json)。样例包含真实本次生成的 Evidence IDs / Snapshot hash / Intent hash，仅含 synthetic 数据；重新 bootstrap 世界会产生不同 Observation / Case 时间与 hash。

## 验收记录

最终共 677 个测试实例，原有 585 个保留，新增 92 个。既有测试只更新 Graph catalog aggregate version 的预期值，资金、Hypothesis、Context、Planner、Runtime 的原断言继续运行。

| 验证 | 最终结果 |
|---|---|
| SQLite full suite | **674 passed / 3 skipped**，201.97 秒 |
| PostgreSQL full suite | **675 passed / 2 skipped**，236.08 秒 |
| Remediation Provider 专项 | **9 passed / 1 skipped**，6.69 秒 |
| 三份实际 Demo JSON Schema / Intent hash 重算 | 全部通过 |
| git diff --check | 通过 |

SQLite 跳过 PostgreSQL 专项与两个可选在线模型测试；PostgreSQL 仅跳过两个在线模型测试。新 Step 7 测试为 91 passed / 1 optional live skipped。真实 OpenAI SDK 的 MockTransport roundtrip 确实执行，没有 importorskip("httpx2")。两套均只有既有 Starlette / AnyIO 弃用警告，没有失败。默认网络完全不参与 Fake / Provider mock 验收；在线修复模型测试须同时显式设置 RUN_LLM_TESTS=1、OPENAI_API_KEY、REMEDIATION_MODEL。本次没有发出真实模型请求。

```powershell
.\.venv\Scripts\python -m pytest -q --tb=short -p no:cacheprovider --basetemp=.local/step7-sqlite-verified
# TEST_POSTGRES_URL 指向专用测试库，每个测试创建并清理独立 schema。
.\.venv\Scripts\python -m pytest --postgres -q --tb=short -p no:cacheprovider --basetemp=.local/step7-postgres-verified
.\.venv\Scripts\python -m pytest tests/test_remediation_provider.py -q
```

无副作用测试对比建议前后的 Case、Evidence、CaseCall 数量、完整隐藏 WorldState，并把执行器方法替换为必失败 Mock。额外 AST 测试禁止 remediation 模块依赖 Simulator / vault / CaseToolExecutor / AgentRuntime / SQLAlchemy 或调用 execute/observe/reserve/pause。S6-ready、S7 redelivery、reconciliation、S8、Provider 失败都执行这些检查。Oracle 仅用于测试断言，不进入模型或服务。

## 实际文件

```text
src/credit_harness/remediation/
  __init__.py       models.py       catalog.py       state.py
  validator.py      deployment.py   preflight.py     renderer.py
  model.py          service.py      audit.py
src/credit_harness/adapters/
  remediation_fake.py              openai_remediation.py
scripts/demo_remediation.py
tests/test_remediation.py
tests/test_remediation_provider.py
tests/support/remediation_fixture.py
docs/remediation-boundary.md
docs/examples/s6-remediation.json
docs/examples/s6-ready-remediation.json
docs/examples/s8-remediation.json
```

另更新 README、.env.example、Tool DTO、Evidence claim/extractor、Context claim 类型与 eligibility version、Graph catalog relevant claims / aggregate version、外部引用 alias，以及两条现有版本断言。没有改 Case Runtime、数据库表、现有 Simulator 场景、正常 Observation projection、Tool dispatch、调查 Planner policy / ranking 或 Agent Loop。

## Step 8 明确未实现

Approval、Capability Token、SideEffectLedger、真正 Idempotency、Domain Command、业务 Side Effect、崩溃恢复、Independent Evaluator 都留待 Step 8 及后续。没有发 Callback、重新消费 MQ、重投资产通知、创建 reconciliation task、新资金意图、改变金额/账户/收款方、Case CLOSED、Shell/SQL/HTTP 写入口。已有 UI 不增加执行按钮；不新建 UI。不接真实数据、不引入 LangGraph / CrewAI / AutoGen，不新增微服务。
