# Step 14 / 14.1 — System Registry、Case Route Revision 与金融接入语义

系统由可信部署人员登记。Agent 不扫描内网，不试 URL，不自行替换 Partner。Registry 回答“当前 Case 可以向哪个已登记来源取得什么事实”；它不是业务事实，也不是执行授权。全部演示系统、账户、订单和身份数据均为 synthetic test fixtures。

## 数据与权限边界

| 对象 | 含义与字段 |
|---|---|
| SystemDefinition | 有限 SystemType、SourceChannel、system_id、display_name、owner_team、status、scope、effective interval；credential_ref 仅服务器可见 |
| CapabilityDefinition | 业务问题类型、ToolName、READ/WRITE、可产生 Claim、可贡献 requirement、lookup、adapter reference、协议契约版本、状态、生效区间与 RecoveryContract |
| RoutingScope | tenant_id、PERSONAL_CREDIT domain、environment、partner_role/id、product_codes、protocol_versions 或明确 version_agnostic |
| ClaimAuthorityRule | 每个 capability × claim 的 AUTHORITATIVE / CORROBORATING / DIAGNOSTIC，以及 subject/identity/freshness/completeness 前置条件 |
| CaseRouteContext | Case/tenant、资产/资金/担保 Partner refs、产品、协议、environment、业务有效时间、明确 draining policy；由可信部署配置绑定 |
| ResolvedCapability | Runtime 私有的 source/adapter、Claim 与 authority rules、registry_version、routing_fingerprint、query/response contract versions；没有 URL、secret、credential_ref |
| CaseCapabilitySnapshot | Case 范围内的抽象 capability、ToolName、claims、authority、cost/latency，以及版本与内容指纹；没有基础设施标识 |

系统和能力共用 `RoutingScope` 与 `EffectiveDefinition`，避免两份 tenant / partner / effective-time 字段漂移。CapabilityDefinition 保留一个业务主类型；例如支付来源以 ESTABLISH_PAYMENT_FINALITY 登记，同时声明真实 Payment DTO 提供的完整身份 Claim。能力的存在与 claim 的权威性分开：FUNDING_INTEGRATION（我司资金接入记录与资金合作方正式接口）可以权威观察 FUND_BUSINESS_STATUS，却不能把 SUCCESS 变成 PAYMENT_FINALITY。注册入口校验 Claim 与实际静态 Tool DTO contract 的集合包含关系，不能通过 metadata 发明 deployed schema 或其他工具不能返回的事实。

`authority.can_support()` 只判断证据是否满足来源规则要求，接受已有确定性身份服务的 MATCH/MISMATCH/UNKNOWN。它不调用 Vault，不比较姓名或手机号，不确认支付，不改变 Evidence strength。现有 Hypothesis / Identity / IndependentEvaluator 的完整 witness 和新鲜度规则继续执行，Registry 不替代它们。

## 路由与唯一性

最终范围是 Registry capability ∩ Case.allowed_tools/forbidden_actions ∩ tenant ∩ domain ∩ partner ∩ product ∩ protocol ∩ environment ∩ business effective time ∩ System/Capability status。任意层不匹配，该来源不可见。

未知 Partner/Product 不猜测；未知 protocol 只允许明确 version-agnostic 来源，例如协议查询入口。版本查询参数仍由原 ToolQuery 验证；查询历史协议 2.2 不会把本 Case 的可信路由协议改成 2.2。所有日期是带时区时间，生效区间为 `[effective_from, effective_until)`，路由 effective_at 是明确注册的业务时间，不是任意当前 wall clock。

DISABLED 总是拒绝；DRAINING 默认拒绝。只有配置 allow_existing_draining 且 Case.created_at 严格早于 definition.draining_since 才可继续；不会把新 Case 当作既有 Case。System 与 Capability 两层分别检查。

按 Claim 解析时使用明确 Authority 顺序，同级多个最佳来源返回歧义。保留 ToolName 的阶段，一次 CALL 不能表达任意 source preference：同一个 ToolName 有多个合法 capability 时也拒绝解析，避免 Planner 只提 PAYMENT，Runtime 却偷偷替它选一个实例。当前未实现通用路由优先级规则；需要可信管理员消除重叠定义。

无支付来源：`NO_REGISTERED_SOURCE_FOR_PAYMENT_FINALITY`。其他缺失：`NO_REGISTERED_SOURCE`。多个权威来源：`AMBIGUOUS_AUTHORITATIVE_SOURCE`。这些来源不会投影为 available_tools；现有 Planner 可针对缺口建议 ESCALATE，Verification handoff 则产生 OPERATOR_FOLLOWUP。Registry 不合成 Tool Candidate、不调用模型、不宣称 UNKNOWN 是失败。缺失或不合法的 CaseRouteContext 是配置错误，整体 fail closed；不会尝试静态来源。

## 持久化与版本

新增 SQL 表：registry_versions、registry_heads、registry_systems、registry_capabilities、registry_claim_authority_rules、registry_audit、registry_case_routes、registry_case_snapshots、registry_dispatch_sources。create_harness_schema 以新增表方式启动旧库，不重写历史 Case/Evidence/Benchmark。

RegistryAdmin 是不挂 HTTP 路由的可信配置入口。先 register 不可变定义，再 activate(version, expected_version, actor)。System、Capability、Authority body 都以 version 为键保存；激活仅 CAS 修改 tenant 唯一 head。并发激活同一旧 head 只有一个成功，另一个得到 STALE_CAPABILITY。审计记录 REGISTERED、ACTIVATED、DRAINING、DISABLED、RETIRED、actor 与时间，不记录 credential 内容。RETIRED 表示被新 head 替换，历史 body 继续保留。

Case routing facts 是 Runtime 配置，不是从 Goal/LLM reason_summary 提取。Step 14.1 用不可变 revision 链推进协议，禁止原地覆盖；具体 proof 和并发规则见下节。Registry 配置本身没有 Agent 可写入口。当前 trusted admin 是进程内组合边界，不冒充完整企业 IAM。

CaseCapabilitySnapshot 按有界、类型化、无基础设施信息的 payload 计算 SHA-256，assembled_at 使用 routing effective_at 逻辑水位。同一 Case/路由/版本/权限得到相同 snapshot_id。快照持久化供重演，旧版本在 Registry 变化后仍可审计。

## Step 14.1：Case Route Revision

新增 `registry_case_route_revisions` 和 `registry_case_route_heads`。Revision 保存 case/tenant、parent、完整 route、routing fingerprint、change reason、supporting Evidence refs、created_at/created_by 和验证状态。revision ID 是规范化内容的 SHA-256；读取时重新校验内容和 scope。`TRUSTED_INITIAL / VERIFIED` 表达来源验证状态，是否 active 只由独立 head 决定。旧 revision 永久保留；不存在更新旧 body 的服务接口。

初始 trusted binding 同时建立 R1 与 head。旧 Step 14 的 `registry_case_routes` 原始 JSON/hash 保留不动；可信部署代码可调用 `RouteUpdater.initialize_legacy(case_id)` 幂等建立 LEGACY_IMPORT R1。未迁移旧绑定仍能只读解析；已存在 revision 却丢失 head 时 fail closed，不能回退旧 UNKNOWN 绑定。相同原始 admin bind 不会覆盖已经推进的 head。

唯一推进入口是进程内可信 `RouteUpdater.promote_protocol(case_id, expected_revision_id=..., evidence_refs=(...))`。调用者只能提供 Evidence ID，不能提供协议值、partner/product 更新或任意 raw context；此服务没有 Agent Tool 或公共 HTTP 注册，Planner、Skill、Experience 无权调用。

UNKNOWN→2.3 的确定性 proof：

1. 从当前 Case 持久化 Evidence 中取得 CURRENT、COMPLETE 的 PROTOCOL_FIELD_TYPE，校验订单、PROTOCOL subject、版本 metadata、source_as_of 与观测时间。
2. 反查 EvidenceOrigin→CaseCall→ObservationRow，核对当前 Case 的 simulation/grant、tool/request、dispatch correlation、Observation ID 与 content hash。
3. 从已持久化 ProtocolData 重新确定性提取，要求与该 Evidence 完全一致；不能只看到字符串“2.3”就更新。多个 proof 字段必须来自同一 Observation，禁止跨次拼装。
4. 核对 RegistryDispatchSource 的 tenant/case/call、注册版本、capability/system/adapter/contract/authority，以及当前 revision 祖先链的 route hash。来源必须仍有效，且为已注册 PROTOCOL_REGISTRY / READ_PROTOCOL_SCHEMA 的权威协议查询能力。
5. 在 Case lock→Registry head lock 内追加 R2，并以 expected revision 对 route head 做 CAS。整个事务失败会回滚新增 revision。并发两个不同版本只允许一个成功，另一个返回 STALE_ROUTE_REVISION。

第一版仅支持 UNKNOWN→KNOWN。再次验证相同协议是幂等 no-op；已知 2.3 后提交 2.2 返回 `ROUTING_FACT_CONFLICT`，保留原 head。可信调用方应将冲突交给人工继续调查；本补丁不自动创建人工 Work，也不改变 Case 生命周期。普通历史协议查询本身不会推进路由。

路由协议使相应版本的查询能力可用，**不等于已证明资金请求实际使用该协议**；FUND_REQUEST_PROTOCOL_APPLICABILITY 仍由独立业务 Evidence contract 判断。推进 R2 改变 routing_fingerprint，旧 Snapshot 在执行前自动 STALE_CAPABILITY；重新组装 Context 才能获得 Messages 2.3。不会合成 Planner Candidate。

## Step 14.1：金融接入与来源范围

`FUNDING_INTEGRATION` 表示我司资金接入系统或合作机构正式接口；Harness 不访问苏商、海尔内部核心数据库。`PAYMENT_STATUS_SOURCE` 表示合法授权的支付最终状态来源，不假设存在可直连的合作方内部支付账本。

`SourceChannel` 是有限枚举：INTERNAL_SYSTEM、INTERNAL_SETTLEMENT、PARTNER_OFFICIAL_API、RECONCILIATION_FILE、PAYMENT_INSTITUTION_API。它描述接入类别，没有 URL、凭据或自动发现能力。只有 PAYMENT_STATUS_SOURCE 且明确配置 AUTHORITATIVE PAYMENT_FINALITY rule 的来源，才能成为权威支付终态候选。注册同时要求 subject binding、identity binding、CURRENT 和 COMPLETE；最终业务结论仍需原 Identity/Evaluator witness，FUND SUCCESS 永远不能替代它。

历史 Registry JSON 先验证原 hash，再进行只读兼容投影：旧 FUNDING_CORE / PAYMENT_LEDGER 映射到新类型，缺少 channel 的旧配置按有限映射补入。历史 payload/hash/version 不重写；新发布必须使用新模型。

| Synthetic Tool | Partner scope | Source channel / 归属 |
|---|---|---|
| PAYMENT / FUND / LOAN_NOTE | FUNDING / SUSHANG | PARTNER_OFFICIAL_API；合法支付结果或我司资金接入来源 |
| ASSET / ASSET_DELIVERY | ASSET / JD | PARTNER_OFFICIAL_API；JD-like asset integration |
| GUARANTEE / ACCOUNTING / TRACE | neutral（两个 partner 字段均为 None） | INTERNAL_SYSTEM；我司内部系统 |
| CALLBACK / CALLBACK_RAW / MESSAGES | neutral | INTERNAL_SYSTEM；我司 Gateway 与消费链路，仍按 Case/order/event 关联 |
| PROTOCOL | neutral、version agnostic | INTERNAL_SYSTEM；我司持有的合作协议登记与发现入口 |

Messages capability 仍限定 2.3；neutral 不等于绕过 tenant、Case、订单或事件 scope。所有上述接口及数据均为 synthetic，不接任何真实资金系统。

## Context / Planner 接入

`ReasoningContextAssembler(catalog=RegistryBackedCatalog(resolver))` 通过可信 catalog 取得兼容 ToolCapability 投影与 CaseCapabilitySnapshot。在 `section_trust.capability_snapshot = TRUSTED_CONTROL` 分区中渲染；既有 facts/history 仍是 UNTRUSTED_EXTERNAL_DATA。Context schema 升为 6，Model Input schema 升为 4。ReasoningContextSnapshot 仍是模型唯一业务输入。

兼容投影复用固定 Tool 说明、风险和数据分类，仅把 produces_claim_types / contributes_requirements 收窄到 Registry 实际登记范围。Agent 看不到整个 Registry、system_id、adapter_id、hostname 或 credential_ref；仅看到当前合法抽象能力及 authority 条件。静态 ToolCapabilityCatalog 暂时保留给未迁移部署与历史测试，不会在已配置 Registry 解析失败时自动兜底。

Context boundary 只导入 Registry 的纯公开模型；SQL 查询在注入的 trusted catalog 中完成。模型看不到私有 metadata 原文，可信 System display_name 等自由文本也不会进入模型输入。身份只使用现有 token/ref 和确定性比较结果，Raw PII 永不用于路由。

## 执行前重验与 provenance

可信组合注入 `RegistryDispatchGuard(CapabilityResolver(repo), TrustedAdapterResolver(bindings))` 到 CaseRepository.registry_guard。Adapter binding 是 `(system_id, adapter_id, query_version, response_version) → server-side factory(case_id)`。工厂使用已有受限 Tool credential，具体 Python 实例显式注册；没有 eval、任意 module import 或动态 URL。缺少 binding 在预算预留前失败。

AgentExecutionPrecondition 携带输入 Snapshot 的 registry_version 和 routing_fingerprint。CaseToolExecutor 仍是唯一调查执行入口。reserve_call 在既有 Case/Work/budget CAS 事务内，按 Case → Registry head 顺序锁定并重新解析当前来源；版本、routing、状态或契约变化即 STALE_CAPABILITY。旧 Case 本身没变化也不能绕过这一步。版本比较是保守的：不相关 Registry 变化也会要求重建 Snapshot。

版本检查与调用准入以 reservation 提交为线性化点；之后已准入的只读调用绑定该版本，不将 Registry 的后来停用描述成能取消已经在途的网络请求。事务不跨 HTTP。已登记 route 的 Case 若缺 guard，Runtime 明确拒绝；不能新建一个 legacy CaseRepository 绕过 Registry 检查。

来源 sidecar 与 CaseCall 在同一个事务提交。然后原 HTTP Observation 服务持久化真实 Observation，原 EvidenceRepository 执行 dispatch correlation / grant / scope / hash 校验及确定性 extraction。追溯路径：

```text
Evidence → EvidenceOrigin → CaseCall → RegistryDispatchSource
                                ↓          → system / capability / adapter / registry_version / route hash
                           ObservationRow → tool / request / content_hash
```

`RegistryDispatchGuard.evidence_sources()` 是 tenant-scoped 私有 inspection 接口。Sidecar 不复制 raw Observation、不填充业务 Evidence，不让 Agent 提交来源 ID，不改变 Simulator projection。dedup Evidence 的每次来源仍通过 EvidenceOrigin 保留。Registry 注册与解析不会制造一条 PAYMENT_SETTLED Evidence。

## Verification、Recovery 与写授权

Verification Requirement 先映射为真实 Claim / UncollectedRequirement，再由同一 CapabilityResolver 解析。Evaluation handoff 和 Verification Worker 都优先使用注入的 Registry；缺失/歧义时转人工或阻断，而不退回静态 Tool。静态 mapping 仅存在于 resolver=None 的迁移模式。Worker 实际查询仍经过 CaseToolExecutor、预算、lease、Observation/Evidence；Evaluator 与唯一关闭入口未改变。

RecoveryContract 只登记 supports_status_lookup、not_found_proves_no_effect、resolver_contract_version，默认 NOT_FOUND 不证明没有效果。没有重写 Step 9 Recovery resolver、ledger、幂等、backoff 或 effect-bound Work。

WRITE metadata 可以登记，但不进入 read capability projection、resolve_tool 或调查执行器。它不能签发 Step 8 的 capability，也不能绕过审批、Identity gate、Evidence binding、Fresh Preflight 或 Step 9。RemediationIntent != Authorization；Registry capability 更不等于 Authorization。

## 后续 Hybrid Retrieval

冻结后续 hard filter contract：tenant_id、business_domain、partner_role、partner_id、product_code、protocol_version、environment、capability_type、claim_types、authority_level、effective time、status。来源 scope 用 product_codes/protocol_versions 集合表达支持范围，Case 用单值匹配；effective time 使用明确半开区间。它们均取自结构化可信配置，Embedding 不得推断或覆盖。后续先执行这些 Hard Filter，再做检索、确定性 rerank、Top-K Context。本阶段没有 embedding/vector index、多 Agent、LangGraph、UI-1、真实资金动作或面试包装，也没有服务发现、完整 IAM、Vault/KMS 或动态插件加载。

## 本地演示与验证

```powershell
.venv\Scripts\python -m scripts.demo_registry > .local/registry-demo.json
.venv\Scripts\python -m pytest tests/test_registry.py tests/test_registry_runtime.py tests/test_route_revisions.py -q
```

Demo 使用 S6 synthetic HTTP 工具产生真实 Evidence，展示 R1 UNKNOWN→Protocol Observation→R2 2.3、旧 Snapshot stale、Messages 解锁、中文接入语义、缺失 Accounting 与双权威 Payment 歧义。输出独立的模型可见快照和 Runtime 私有来源追溯；创建独立 `.local/registry-demo-*.db`，不覆盖历史演示数据库。PowerShell 重定向中文输出时建议先设置 `$env:PYTHONIOENCODING='utf-8'`。

Step 14 历史基线（1270 collected；原 1231 项 + 新增 39 项）：

| 验证 | 结果 | 耗时 |
|---|---|---|
| Registry + Runtime 定向，SQLite | 39 passed | 18.75s |
| Registry + Runtime 定向，PostgreSQL | 39 passed | 35.36s |
| SQLite full suite | 1267 passed, 3 skipped | 1316.73s |
| PostgreSQL full suite | 1268 passed, 2 skipped | 1563.10s |

两项 live Planner / Remediation 测试未显式开启；SQLite 另跳过 PostgreSQL 专项。离线 Provider + MockTransport 结构化输出测试实际执行。每套测试只有既存 Starlette/AnyIO BlockingPortal 弃用提示。`git diff --check` 通过。

历史演示的 Messages 来源已在 Step 14.1 调整为我司内部 neutral scope；当前结果以下方 Step 14.1 验证为准。

### Step 14.1 实际演示

`scripts/demo_registry.py` 当前实际输出摘要（完整结果含 revision hash、Evidence refs 与来源 sidecar）：

```json
{
  "case_id": "CASE-JD202609100001",
  "route_progression": [null, "2.3"],
  "messages_available_before": false,
  "messages_available_after": true,
  "old_snapshot_revalidation": "STALE_CAPABILITY",
  "observed_evidence_count": 22,
  "business_meanings": {
    "get_payment_transaction": "合法授权的支付结果来源（synthetic official API）",
    "get_fund_order": "我司资金接入系统 / 苏商官方接口（synthetic）",
    "get_asset_order": "JD-like asset integration（synthetic）",
    "get_messages": "我司内部 messages（synthetic）"
  },
  "missing_accounting": "NO_REGISTERED_SOURCE",
  "ambiguous_payment": "AMBIGUOUS_AUTHORITATIVE_SOURCE",
  "write_authorized": false
}
```

以上 route_progression 与 messages_available 是根据真实 revision 和前后 capability 列表整理的摘要字段；原始 Demo 返回完整列表，不把这些摘要变成业务 Evidence。

### Step 14.1 测试记录

新增 29 项，合计 1299 collected。既有测试仅更新新 SystemType 名称，没有删除或放宽安全断言。

| 验证 | 结果 | 耗时 |
|---|---|---|
| Registry + Runtime + Route revision，SQLite | 68 passed | 32.99s |
| Registry + Runtime + Route revision，PostgreSQL | 68 passed | 59.76s |
| CAS / Worker lease / 原子性定向，SQLite | 15 passed, 141 deselected | 29.36s |
| CAS / Worker lease / 原子性定向，PostgreSQL | 15 passed, 141 deselected | 42.41s |
| Agent Runtime / Evaluator / Context / Orchestration / Recovery 专项，SQLite | 497 passed | 709.57s |
| SQLite full suite | 1296 passed, 3 skipped | 1385.72s |
| PostgreSQL full suite | 1297 passed, 2 skipped | 1727.98s |

全量日志：`.local/step141-full-sqlite.log`、`.local/step141-full-postgres.log`；真实 Demo：`.local/step141-demo.json`。两项可选 live LLM 测试未开启；SQLite 另跳过 PostgreSQL 专项。离线 Provider / MockTransport 测试实际执行。每套仅有既存 Starlette/AnyIO BlockingPortal 弃用提示；`git diff --check` 通过。没有运行 Step 15、Vector/Embedding、UI 或真实资金操作。
