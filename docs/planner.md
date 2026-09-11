# Step 5 — Proposal-only Next Best Action Planner

本阶段首次提供可选 LLM Provider。模型只建议下一步，Harness 验证并选择建议，随后停止。默认 Fake 模型离线运行，所有身份和金融数据均为 synthetic fixtures。

```text
ReasoningContextSnapshot
  → ModelInputRenderer / ReferenceAliasProjector
  → PlannerModel → PlannerDraft (1–3 candidates)
  → CandidateValidator → HardPolicyFilter
  → DeterministicActionRanker
  → PlannerDecision + ValidatedActionProposal + PlannerAuditRecord
  → STOP
```

## 唯一输入与模型权限

`PlannerService.plan(snapshot)` 和 Renderer 只接受确切类型的 ReasoningContextSnapshot。Renderer 重验完整类型化 envelope、内容指纹和预算；不接受 CaseEvidenceView、HypothesisGraphView、Evidence 列表、Observation、原始 Callback、Oracle、聊天记录或任意文本上下文。Snapshot 是可信 Runtime 装配的输入，不是外部上传 JSON 的授权入口；其 hash 是完整性标识，不是签名或权限凭证。

Planner 没有 CaseToolExecutor、Tool Client、SimulatorAdmin、数据库连接或上游 credential 依赖；Provider 没有注册业务 tools/functions。Tool Capability 是能力描述，不授予 Tool Permission。建议通过后也没有执行回调。现有 Harness Tool Gateway 仍是未来执行时必须经过的边界。

## Model Input Trust Boundary

ModelInputBundle 使用独立冻结的 Pydantic 分区模型，不把所有字段合并为一段 Prompt：

| 字段 | 来源与用途 |
|---|---|
| system_contract | prompt_contract.py 中静态常量；不插入运行时数据 |
| trusted_control | TaskContract、FinancialSubject、Safety、Budget、available_tools、内部订单标识 |
| deterministic_derived | Financial Identity、Hypothesis capsules、resolved summary、open gaps |
| untrusted_external_data | 当前 FactCapsule、HistoryDigest；命令式文本仍只是数据 |
| planner_output_schema | 代码中的 PlannerDraft JSON Schema |

Renderer 逐字段读取 section_trust 分配区域，Literal 标记不能提升外部数据。OpenAI Adapter 把静态 contract 放 system 消息，分区 JSON 放一条 user 消息；不携带 previous_response_id、聊天历史或额外 raw context。

静态 contract 明确 UNKNOWN ≠ FAILED、Tool SUCCESS ≠ Business Outcome，禁止修改 Goal/Safety、发明 Tool、执行动作或宣布结案。它只是行为指导。安全仍由 Schema、Validator、Policy 确定：即使 Fake adversary 服从 IGNORE_PREVIOUS_INSTRUCTIONS、提出另一个订单或虚构工具，Harness 仍会拒绝。

## External Reference Alias

ReferenceAliasProjector 对外部 transaction/request/message/callback/loan/protocol subject 引用按原值排序映射 EXTREF-001 等。覆盖当前事实、历史首末引用值和 Identity transaction preview；相同原值映射同一 alias，不同值不共用 alias。已有同名字面量会被跳过，防止碰撞。

只改写引用类型的字段。外部 identifier 恰好叫 SUCCESS 时，不改变 Fund 状态 SUCCESS。内部 order/control 参数、协议版本以及 CUS/BEN/ACC tokenized identity 保留：前者用于严格 query 校验，后者本来就是最小化后的身份关联引用，不包含 raw PII。

原始 Snapshot、Evidence 和 durable provenance 不修改。alias → original 的只读映射属于 Renderer 的 Runtime 私有状态，ModelInputBundle、model_visible_payload、Provider request 和 Planner Audit 均不包含它。alias 不用于授权，也不会被还原后直接执行；本阶段 ProposalQuery 没有外部 ID 查询字段。

Alias 解决引用暴露，不声称是通用 DLP。错误码等仍依赖 Step 4.2 的结构化资格边界；未来新增字段必须显式分类。Snapshot 字符预算也不等于 Provider tokenizer 限额：静态 contract、输出 schema 有额外开销；Provider 超限时 fail closed，不能截断关键事实。

## Candidate 与 Validated Action

模型继承 extra=forbid、frozen=True。集合有界，仅允许三种动作：

| 类型 | 关键字段 |
|---|---|
| CALL_TOOL | candidate_id、target_gap_ids、ToolName enum、ProposalQuery、expected ClaimType enums、简短 reason_summary |
| WAIT | target_gap_ids、ReasonCode enum、30–3600 秒 suggested_wait_seconds、简短解释 |
| ESCALATE | target_gap_ids、ReasonCode enum、可选 UncollectedClaimType requested_capability、简短解释 |

ProposalQuery 仅有 internal_order_id、protocol_version、effective_at，是不可信参数建议，不是 ToolQuery。Draft 必须给出输入 snapshot_id、1–3 个唯一 ID 的 Candidate、短 observation_summary/uncertainty_summary。score/confidence、private chain_of_thought、Write/Repair/Close 等多余字段或动作均无法解析。整个 Draft 无效时抛 PlannerProtocolError，不从自然语言猜动作。

ValidatedActionProposal 保存 snapshot_id 与 policy_version，是通过当前检查的建议，不是 Capability Token。它不代表以后可以省略实时权限、版本和预算检查。

## Candidate Validation 与 Gap / Capability Mapping

每个 target 必须是当前 Snapshot 中真实的 OPEN Gap。CALL_TOOL 必须使用 available_tools 内名称、精确内部订单号，expected claims 是该能力 produces 的子集。非 PROTOCOL 不接受协议/时间参数；当前 PROTOCOL 仅允许已在 Snapshot 中观测到的版本，effective_at 必须为 null，避免任意扩大版本和时间。其他协议调查范围应由后续 Runtime 明确提供，不由模型发明。

CALL_TOOL 至少对一个 target 满足：produces 与 required ClaimType 有交集，或 contributes_requirements 与 required UncollectedClaimType 有交集。无关 accounting 查询会得到 TOOL_DOES_NOT_ADDRESS_TARGET_GAP，即使它是合法只读 Tool。

| Tool | 额外可贡献 requirement |
|---|---|
| GUARANTEE | REQUEST_ASSOCIATION、FUND_REQUEST_PROTOCOL_APPLICABILITY（真实 fund_request_id / protocol_version 元数据） |
| TRACE / FUND / PAYMENT | REQUEST_ASSOCIATION |
| CALLBACK / CALLBACK_RAW / MESSAGES | CALLBACK_EVENT_ASSOCIATION |
| 其他 | 无额外声明 |

贡献不保证满足 Gap，更不保证查询成功。没有任何能力提供 DEPLOYED_CONSUMER_SCHEMA_VERSION；Message expected type 与旧协议相符不能冒充部署版本。模型声称 MESSAGES 能解决该 Gap 会被拒绝；可提出 NO_AVAILABLE_TOOL / UNSUPPORTED_INVESTIGATION 的 ESCALATE。

## Hard Constraint 与 Ranking

HardPolicyFilter 独立重查 Candidate 约束，拒绝不可调查状态、预算耗尽、非 READ_ONLY、越权工具/订单、无效 Gap/不兼容能力、连续相同失败达到阈值、查询历史不完整。全部是 REJECT，不是负分。模型说“最安全”不会改变结果。

通过硬约束之后按稳定整数元组做字典序排序：

1. Gap priority：SAFETY_CRITICAL=3、DISCRIMINATING=2、SUPPORTING=1。
2. 尚未在当前完整新鲜事实中出现的 claim/requirement 覆盖数。
3. 总 requirement 覆盖数。
4. 关联且尚未确认/排除的 Hypothesis 数量。
5. LOW 成本/LOW 延迟的整数档位。
6. 阈值以下的重复失败惩罚，最后按 candidate_id 稳定打破平局。

Risk 不参与评分。新颖覆盖是可检查的启发式，不是精确的信息增益或结论概率。OPEN Gap 的未采集关联 requirement 按潜在贡献计数；实际满足情况仍由新 Evidence 重算。

Step 5.1 的 Safety-Critical Omission Gate 检查完整 Snapshot，而非模型候选集合：只要存在 OPEN + SAFETY_CRITICAL Gap，且 available_tools 中至少一个 capability 能 address，它就是 actionable safety gap。未实际覆盖至少一个这样的 Gap 的 CALL_TOOL 在排名前被拒绝，拒绝码为 ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED。只添加 safety target ID，但 Tool 没有对应能力，也不能通过。

Actionable 按 Snapshot 的能力描述判断，不因模型省略候选而消失；预算、连续失败等仍由各自独立硬约束拒绝。没有 capability 能 address 的 safety gap 不触发此 Gate。模型未提出合法 safety call 时，针对至少一个 actionable safety gap 的合法 WAIT/ESCALATE 可继续排名；否则 selected_action=null。Harness 不补造遗漏的 PAYMENT 或其他 Tool Candidate。

Step 5.1 final patch 将相同的 actionable safety 集合用于三个动作类型：CALL_TOOL 必须通过自身 capability 实际 address 至少一个该 Gap；WAIT/ESCALATE 的 target_gap_ids 至少包含一个该 Gap，否则硬拒绝 ACTIONABLE_SAFETY_GAP_NOT_TARGETED。只处理其他低优先级缺口的等待或升级不能被选中。

采用保守规则：即使预算耗尽、调查被禁止、重复失败或历史不完整阻止了所有安全查询，WAIT/ESCALATE 仍须指向安全缺口。不实现“所有 CALL 已被其他政策阻断”的豁免逻辑。无 actionable safety gap 时，此额外限制不生效；原有 target 存在性、capability binding 与 reason 一致性检查继续生效。

例如完整 S6 中，单独针对 DEPLOYED_CONSUMER_SCHEMA_VERSION 的 NO_AVAILABLE_TOOL 升级虽然满足 capability binding，仍会因遗漏可调查的 FUND_PROTOCOL_APPLICABILITY safety gap 被拒绝。不能把“某一个非安全 Gap 缺工具”当成忽略安全缺口的理由。

WAIT/ESCALATE 对已被合法 CALL 覆盖的同一 Gap 不抢先；对没有合法候选覆盖的更高优先级缺口，可胜过低优先级调查。CALL_TOOL 不是无条件高于 ESCALATE。排序只比较硬过滤后剩下的模型候选，不自行生成 Tool Call。

## Step 5.1：Escalation Capability Binding

EscalateCandidate.requested_capability 非空时，必须是 UncollectedClaimType，且出现在至少一个 target gap.required_claim_types 中，否则拒绝 ESCALATION_CAPABILITY_MISMATCH。部署 schema capability 可针对 DEPLOYED_CONSUMER_SCHEMA_VERSION Gap 请求，不能借 PAYMENT_FINALITY Gap 请求；普通 ClaimType 不能伪装成待采集 capability。

NO_AVAILABLE_TOOL 也由 Harness 检查：所有 target gap 都必须没有 available capability 能 address。只要其中一个可 address，就拒绝 ESCALATION_REASON_INCONSISTENT；混合可解决和不可解决的 target 应拆开表达。其他 reason_code 不因此获得额外权限，reason_summary 始终不参与判断。

Step 5.1 初始补丁将 PLANNER_POLICY_VERSION 从 1 升为 2；Safety Fallback Target Integrity final patch 再升至 3。Decision、ValidatedActionProposal、Audit 与 decision hash 随之绑定新策略。Ranking、输入 Snapshot、执行边界保持原设计。

## Repeated Query Protection / WAIT / ESCALATE

Context schema 升至 4、compaction 升至 3、context policy 升至 4；eligibility 仍为 3。RepeatedLookupGroup 累计 count 继续保留，另增 latest_consecutive_count：按同 Tool + 精确 query scope，以 Observation 去重并考虑中间成功，计算最近同类型失败的连续次数。相同时刻混合不同结果没有确定顺序，不能伪造先后关系。

HistoryDigest 增加 lookup_history_complete。预算/eligibility 省略查询历史，或者 Evidence 去重后保留的 Observation 来源数不足以覆盖 Case.used_tool_calls，都会使它为 false。此时保守拒绝全部 CALL_TOOL，返回 INCOMPLETE_LOOKUP_HISTORY，仍允许 WAIT/ESCALATE。不会把遗漏或去重当作没有失败；不为补足计数而访问原始 Observation。完整性和连续次数由 Context Invariant Validator 重验。

同 Tool/scope 连续相同失败至少 3 次，拒绝 REPEATED_NO_NEW_INFORMATION。更换 candidate ID、解释文字或模型不能清零；非 PROTOCOL 塞入版本参数也不能规避。三次 Timeout 仍是 UNKNOWN，不变成 NOT_EXECUTED、FAILED 或 SETTLED。

WAIT 适合暂时的数据源不可用，30–3600 秒只是建议，**不 sleep、不 schedule**。ESCALATE 适合预算耗尽、缺工具、范围阻断、证据不足或持续来源失败。本阶段不实现 WAIT 后恢复、新事件重启、source recovery signal 或自动重试。

## Provider Boundary 与错误处理

PlannerModel Protocol 只暴露 plan(ModelInputBundle) 与白名单 metadata。FakePlannerModel 可脚本化返回 Draft、非法 JSON、越权动作或异常，单元测试不依赖网络。

可选 adapters/openai_planner.py 使用 [OpenAI Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 的 responses.parse(text_format=PlannerDraft)。SDK 只在基础设施模块延迟导入，模型从 PLANNER_MODEL、密钥从 OPENAI_API_KEY 读取。设置 store=false、30 秒超时、max_retries=0；无业务 Tool 定义、自动调用、会话续接或隐式重试。

Timeout、Rate Limit、Provider Error → PlannerUnavailable；无效结构、空 Draft、拒绝/不完整输出 → PlannerProtocolError（其子类）。错误不回显 SDK 异常原文，不选择任意 fallback Tool。没有合法候选时 selected_action=null，返回所有拒绝理由。本阶段未新增 Planner HTTP API；未来入口必须绑定可信 Snapshot 和 Case 权限。

## Audit 与 Chain-of-Thought

Decision 分别保存合法/被拒候选、拒绝码、选中建议、排序分项、确定性选择原因、Draft hash、模型 metadata、schema/policy/ranking/input 四个版本。逻辑时间来自 snapshot.assembled_at，不冒充 wall-clock 执行时间。

PlannerAuditRecord 记录 decision/case/snapshot ID、输入输出 hash、版本、选中建议、拒绝记录、provider/name、usage token 计数。当前是 append-only 内存 store，通过 Protocol 可替换持久层；进程退出不保留，不宣称生产审计耐久性。同输入/Draft/策略可重放一致，重复审计调用仍追加。Provider 失败返回类型化错误，当前不生成成功 Decision 审计。

模型 rationale 仅供人类阅读，不能影响权限或生成 Evidence。不请求、读取或持久化 Provider hidden reasoning/private chain-of-thought；仅访问 output_parsed、status、输入/输出 token 计数。API Key、alias map、原始 SDK payload 均不进入审计。

## Demo、测试与 Step 6 边界

```powershell
.\.venv\Scripts\python scripts/demo_planner.py --provider fake --stage all --output .local/planner-stages.json
.\.venv\Scripts\python -m pip install -e '.[test,llm]'
# OPENAI_API_KEY 与 PLANNER_MODEL 由本地环境安全配置，不写入仓库。
.\.venv\Scripts\python scripts/demo_planner.py --provider openai --stage S6-A
.\.venv\Scripts\python -m pytest -q
# 显式选择在线测试；默认 CI 不调用 LLM。
$env:RUN_LLM_TESTS = '1'
.\.venv\Scripts\python -m pytest -m llm -q
```

Demo 外层先通过真实 Harness HTTP 路由完成指定人工步骤，再生成 Snapshot；只有 trusted setup 知道 Scenario。Planner 不获取 Scenario，不执行返回动作。Fake 是脚本化建议，用于演示拒绝和排名，不能冒充在线模型能力评估。

| Snapshot 阶段 | 所选建议 | 依据 |
|---|---|---|
| S6-A：仅 TRACE | PAYMENT | PAYMENT_FINALITY safety gap；尚未查询支付 |
| S6-B：TRACE + PAYMENT | GUARANTEE | Identity MATCH、支付已知，调查资产收敛中的我方状态 |
| S6-C：再查 CALLBACK | MESSAGES | Gateway 已到达，消费状态未知 |
| S6-D：七步完整调查 | GUARANTEE | 补资金请求协议 applicability；重复 Payment 不解决该 Gap |
| S8-initial：只有 FUND | PAYMENT | 支付尚未查询 |
| S8-repeated：Fund/Payment 各三次失败 | ESCALATE | 重复 Payment 被硬拒绝，UNKNOWN 保留 |

完整实际 Snapshot、Decision、Audit 见 [六阶段输出](examples/planner-stages.json)。accounting 候选均被拒为不相关。模型可见分区见 [S6 Renderer 输出](examples/s6-planner-model-input.json)，不含 alias map。

Step 5 终点是 ValidatedActionProposal。Step 6 才能在重新校验实时 Case 权限、预算、状态和 Snapshot 时效后，重新构造 ToolQuery 并提交现有 Runtime。本阶段无 Agent Loop、Write Tool、Repair、Capability Token、Side-effect Ledger、Human Approval 或自动结案，也没有 LLM → CaseToolExecutor.execute 的代码路径。

## Step 5 历史验收记录（2026-09-11，policy 1）

- 新增 Planner / Provider 测试：78 passed、1 skipped；包含真实 Harness HTTP 阶段测试、恶意 Fake、别名一致性/碰撞、去重历史保护，以及真实 OpenAI SDK 3.11.0 的离线 MockTransport 结构化输出往返。
- SQLite 全量：486 passed、2 skipped，107.62 秒。跳过 PostgreSQL 专用测试与可选在线 LLM 测试。
- PostgreSQL 全量：487 passed、1 skipped，154.60 秒。仅跳过在线 LLM。
- 单独运行 `pytest -m llm`：1 skipped、487 deselected。环境未提供完整 OPENAI_API_KEY / PLANNER_MODEL 配置；没有实际发出付费模型请求，不宣称验证了真实模型选策质量。
- 两套测试各有一条既有 Starlette/AnyIO 弃用警告，无失败。Step 0–4.2 原有 393 个测试实例保持通过语义；当前工作区全量还包含其他已有模块测试。
- S6/S8 Context 使用原保存 inputs 重建为 schema 4，反序重放结果一致；六阶段 Planner Demo 通过真实 Simulator/Harness 路由重新生成。
- `git diff --check` 通过。模型成功返回与 Timeout 路径均断言未执行 Tool、未修改 Case/Evidence；Planner 包的依赖测试禁止执行器/Simulator/Tool Client 引入。

## Step 5.1 历史验收（policy 2）

Provider 离线测试明确使用 `pytest.importorskip("httpx")` 与真实 `httpx.MockTransport`。本地已安装 openai 3.11.0、httpx 0.28.1；没有更换 SDK 或修改 Provider 实现。测试断言只发出一次被 MockTransport 截获的 HTTP 请求，启用 strict JSON Schema、未注册业务 Tool，并完成响应解析、Harness 选择与 usage 审计；在线 LLM 测试单独跳过。

新增 16 个 selection integrity 测试覆盖 omission、禁止合成候选、WAIT/ESCALATE 回退、伪造 safety target、不可解决/非 OPEN safety gap、capability 类型与 target 绑定、NO_AVAILABLE_TOOL 一致性。原 safety ranking 测试升级为断言无关候选被硬拒绝。

- `pytest tests/test_planner_provider.py -q`：10 passed、1 skipped，2.48 秒。只跳过在线 LLM，离线 Structured Outputs roundtrip 实际执行。
- 单独运行 `test_sdk_structured_output_roundtrip_offline`：1 passed，2.91 秒。
- Planner / Selection Integrity / Provider 合计：94 passed、1 skipped，6.97 秒。
- SQLite 全量：505 passed、2 skipped，126.49 秒；跳过 PostgreSQL 专用测试和在线 LLM。
- PostgreSQL 全量：506 passed、1 skipped，189.19 秒；仅跳过在线 LLM。
- `pytest -m llm`：1 skipped、506 deselected；本次未调用真实模型。

以上为 policy 2 的历史验收结果；上面的 policy 1 数字及六阶段 JSON 保留为 Step 5 历史记录。本阶段没有新增 Tool 执行入口、Agent Loop 或 Step 6 功能。

## Step 5.1 final patch：Safety Fallback Target Integrity

PLANNER_POLICY_VERSION 升至 3，Decision、ValidatedActionProposal 与 Audit 绑定新策略；Ranking、Provider 与执行边界不变。新增 11 个测试实例，覆盖 WAIT/ESCALATE 忽略安全目标时拒绝、正确指向安全目标时允许、预算阻断下仍保留目标约束、混合目标、无 actionable safety 的场景，以及 S6 部署版本升级不能挤占资金协议安全缺口。

保留“模型没有提出合法 safety CALL，针对该 safety gap 的 WAIT/ESCALATE 可以被选择”的行为。所有候选均被拒绝时，selected_action=None，排名为空，不生成替代候选。原部署版本升级的允许测试使用无 actionable safety capability 的 Snapshot，独立验证升级 capability 绑定；另有完整 S6 的拒绝回归。

- Planner selection / Planner / Provider 回归：105 passed、1 skipped，9.38 秒；只跳过在线 LLM。
- SQLite full suite：516 passed、2 skipped，126.81 秒；跳过 PostgreSQL 专用测试与在线 LLM。
- PostgreSQL full suite：517 passed、1 skipped，174.24 秒；仅跳过在线 LLM。
- `git diff --check` 通过；两套全量各有一条既有 Starlette/AnyIO 弃用警告。本补丁未新增 Tool 执行或候选生成路径。
