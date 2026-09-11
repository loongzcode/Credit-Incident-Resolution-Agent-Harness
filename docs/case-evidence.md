# Case Runtime 与 Evidence Store v1

## 范围与目录

在既有 Simulator 上新增模块，不修改业务世界、Fault Projection 或只读 Tool Grant。`cases/` 管理调查任务、执行入口与预算；`evidence/` 管理观测到的原子事实和来源。没有将任务契约塞进 Simulator，也没有引入通用工作流框架。

```text
src/credit_harness/
  cases/
    models.py          # Case / TaskContract / Scope / Constraints / Budget
    tables.py          # CaseRow / CaseCallRow
    repository.py      # 租户隔离、Grant 约束、原子预算预占
    service.py         # 可信任务创建、读取、暂停
    fixtures.py        # JD202609100001 调查契约，不含场景答案
    executor.py        # 唯一调查工具调用入口
    evidence_view.py   # CaseEvidenceService / CaseEvidenceView
    schema.py          # 仅新增四张 Harness 表
  evidence/
    models.py          # Evidence / ClaimType / Subject / provenance DTO
    extractor.py       # 按 ObservationData 类型确定性提取
    tables.py          # EvidenceRow / EvidenceOriginRow
    repository.py      # 已持久化观测校验、去重、原始观测回溯
    services.py        # Freshness / 三条保守 Conflict 规则
  api/harness.py       # 独立 Harness HTTP 应用，无 Admin 路由
scripts/demo_case_evidence.py
tests/test_case_evidence.py
tests/test_harness_integration.py
docs/examples/s6-case-evidence.json
docs/examples/s8-case-evidence.json
```

## Case 与 Simulation

`simulation_id` 标识一个模拟世界；`case_id` 标识一次调查。同一世界可以有多个 Case，每个 Case 拥有自己的契约、权限、预算、状态和证据。全局唯一的 `case_id` 是所有 Harness 表与查询的第一层边界；Repository 再用由可信配置决定的 `tenant_id` 限制读取。接口不接受请求方自报的 tenant 或 simulation 作为授权依据。

Case 创建属于可信 provisioning：`CaseService.create(case, tool_credential=...)` 校验 simulation 存在、现有 grant 有效以及 scope 为其子集。数据库只保存 grant 哈希，Case DTO 不包含凭据。当前上游 grant 限定单订单，因此 Case scope 也只能是它的子集；多订单授权留待将来上游支持。

TaskContract 包含 goal、success_criteria、stop_conditions、escalation_conditions、forbidden_outcomes。Case.goal 必须与 contract.goal 一致。文本契约保留给未来 Runtime/Agent；目前机械执行的是状态、订单、工具枚举和预算，不声称已经理解自然语言契约。所有可调用工具均来自现有只读白名单。

NEW 的首个合法调用进入 INVESTIGATING；只有这两个状态允许调用。可信服务可以暂停至 WAITING / ESCALATED。CLOSED 是模型枚举，当前没有结案方法或 HTTP 路由，也没有恢复/自动升级的复杂状态机。

## 调用与事务边界

```text
CaseToolExecutor.execute(case_id, tool, query)
  → tenant / case / status / order / tool 检查
  → 条件 UPDATE 原子占用一次 used_tool_calls，写 DISPATCHED receipt
  → 现有 HTTP Tool Client，仍使用原有 simulation/order/tool credential
  → Simulator 在返回前提交 ObservationRow
  → 校验 observation_id、原始哈希、tool、request、grant、simulation 与 receipt
  → deterministic extraction
  → 同一事务绑定 CaseCall、写 Evidence、写全部 origins
  → 返回 Observation + evidence_refs
```

预算在调用前预占，避免两个 worker 同时检查余量后超支；超时和上游 HTTP 异常都计为一次已分配尝试。scope 拒绝不扣预算。预占后进程崩溃会保留预算和 DISPATCHED 记录，不自动重试或退款；当前全是只读调用，没有金融 Side Effect。这个小型调用收据不是 Durable Runtime，也不承诺自动崩溃恢复。

Simulator 返回的 TIMEOUT 是已持久化 Observation，可提取查询证据。Harness 到 API 的本地连接异常只留下 ERROR receipt，不凭空生成业务 Observation。Evidence 写入失败时预算仍消耗、原 Observation 保留；本阶段不做重试队列。

每个 Observation 最多绑定一次 CaseCall（数据库唯一约束），防止将旧响应重放到另一调查。证据写入使用 Case 级数据库串行化，避免并发重复插入。没有跨网络长事务。

## Observation 与 Evidence

Observation 记录“某工具在某时返回了什么”；Evidence 提取“该观测直接支持的可追溯事实”。例如 FUND.SUCCESS 只生成 FUND_BUSINESS_STATUS、LOAN_NO_PRESENT 和可选借据引用；绝不生成支付终态。支付金额来自 Payment.transaction，以整数分保存。协议中 `SUCCESS = LOAN_CREATED_PAYMENT_SEPARATE` 作为协议语义证据保存，当前不执行资金判定。

Callback 成功查询支持网关收到及验签事实；NOT_FOUND 只产生 SOURCE_LOOKUP_STATUS。Timeout 同样只产生查询状态，不产生 FAILED / NOT_EXECUTED。查询证据携带原 tool、query scope、observed_at、completeness、freshness；NOT_FOUND 的含义限于当前查询范围未观察到记录。

MQ 的 `CALLBACK_SCHEMA_MISMATCH` 是 MESSAGE_ERROR_CODE，`loanNo` 是 MESSAGE_ERROR_FIELD。它们不是 ROOT_CAUSE。不同协议版本用 `partner@version` subject，并同时保留 protocol_version / source_version 和原查询参数。无法从可见 DTO 获取的版本保持 null，不读取隐藏快照版本。

Evidence、Subject、Metadata、Case 等使用 `extra=forbid`、`frozen=True`；集合尽量使用 tuple/frozenset。View 的分组字典和既有 Observation 内部 DTO 是普通 Pydantic 容器，并非内存安全沙箱；进程边界才是权限边界。

Strength 仅是元数据。本版根据可见来源与 freshness 保守标注 STRONG / SUPPORTING / WEAK / HINT；枚举保留 AUTHORITATIVE，但没有任何“强证据自动证明已支付”的规则。

## Provenance 与去重

`Evidence → observation_id → ObservationRow → tool / request / content_hash`。`raw_ref` 为 `observation://{id}`，`content_hash` 继承原 Observation 的 SHA-256。哈希规范与原 Simulator 一致：Python `json.dumps(payload, sort_keys=True)` 的 UTF-8 编码。哈希用于完整性校验，不替代数据库访问控制或数字签名。

`EvidenceRepository.get_raw_observation(evidence_id, case_id=...)` 必须同时带 case_id，且 Repository 绑定 tenant，避免通过泄露的 Evidence ID 跨 Case 取原文。原始内容仅存 ObservationRow，不复制到 Evidence。只允许由持久化调用收据进入写入逻辑，没有接收任意 Evidence JSON 的 API。

Evidence ID 使用 SHA-256 deterministic fingerprint：case、tool/source_kind、query、claim、subject、value、event_time、source_as_of、版本、提取器版本与观测质量。排除 observation_id / observed_at / raw hash 等每次调用标识；没有业务 event_time 的查询证据额外包括 observed_at。同一来源、时刻、版本完全相同可以去重；时间或版本变化保留。

去重的 Evidence 指向首个原始观测，EvidenceOriginRow 保留每次调用；`origin_observation_ids` 可查看所有来源，CaseCall 也完整保留重复查询。对同一 Observation 重复运行 extractor，输出完全一致；created_at 使用其 observed_at，代表在模拟观测时钟上的逻辑提取时刻，不冒充独立采集的业务事件时间。

## Freshness、冲突与 View

`is_stale` 仅检查继承的 STALE 标记，不引入业务 TTL。UNKNOWN 不等于 CURRENT。`age(evidence, now)` 使用 source_as_of；缺失时返回 None，拒绝 naive datetime 或倒退时钟。调用者应使用同一时钟域。Case 生命周期时间和冲突检测时间使用 Runtime UTC 时钟；事件时间和可见性时间继承模拟时钟。STALE 证据不删除。

冲突规则仅比较同一 Case、同一 transaction subject、同一业务时间点的 PAYMENT_AMOUNT、PAYMENT_CURRENCY、TRANSACTION_FUND_REQUEST_ID。需要不同 observation_id 且不同可见来源身份 `(tool, source_kind)`，双方 CURRENT / COMPLETE，value 互斥。不同时间的状态变化不是冲突；本版不猜测时间区间重叠，不把重复查询一个来源当成独立来源。现有 Simulator 没有两个 CURRENT 支付来源，所以真实冲突规则用显式合成来源做单元测试，不伪造实际 S6 Evidence。

CaseEvidenceView 包含 case、evidence_count、latest_observations、evidence_by_claim_type、conflicts、unknown_lookups。latest 按 tool + 完整 query 分组，两个协议版本不会覆盖彼此。

额外的 `payment_finality_evidence` 只表示最近支付观测是否提供当前明确字段及其引用，**不是资金终态验收结论**。S8 显示 UNKNOWN、空 evidence_refs，且没有 PAYMENT_FINALITY claim。后续超时不会删除过去的 SETTLED 历史事实，也不会继续将旧查询当成最近支付观测。跨多个服务查询的 View 是读取时汇总，不承诺并发下所有表的事务一致快照。

## 运行与 API

已有虚拟环境中执行（或先 `pip install -e '.[test]'`）：

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python scripts/demo_case_evidence.py --output .local/s6-case-evidence.json
.\.venv\Scripts\python scripts/demo_case_evidence.py --scenario S8 --output .local/s8-case-evidence.json
```

不带 `--output` 则打印完整 JSON。每次 Demo 创建新的 SQLite 文件并保留 provenance，不覆盖既有数据。Demo 使用进程内 HTTP transport 实际调用两个 FastAPI app 的路由，无需启动端口。S6 按 TRACE → FUND → PAYMENT → CALLBACK → MESSAGES → PROTOCOL 2.3 → PROTOCOL 2.2 顺序；S8 重复 Fund / Payment 三次并推进模拟时钟。脚本中的 Admin 仅用于可信 seed/grant/clock，不传给 Harness。

真实分进程部署时用已有 `ToolClient(base_url, token)` 注入 `CaseToolExecutor`；用服务器配置将每个 Case 绑定到对应 Client。创建 `HarnessBinding(executor, CaseEvidenceService(repository))`，将独立 Harness bearer secret 的 SHA-256 映射传入 `create_harness_app`。这是最小静态身份配置，不是 JWT Capability 系统，不能把上游 Tool credential 发给未来 Agent。

公开路由：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/cases/{case_id}` | 读取已配置 Case |
| POST | `/cases/{case_id}/tools/{tool}` | 经 CaseToolExecutor 调用 |
| GET | `/cases/{case_id}/evidence` | 证据汇总 |
| GET | `/cases/{case_id}/evidence/{evidence_id}/raw` | 当前 Case 原始观测回溯 |

原 Simulator `/tools/{tool}` 应作为可信内部服务，只向 Harness 发放凭据。未来 Agent 只持 Harness 凭据并访问 Harness API，不能获得数据库、项目文件、SimulatorAdmin、上游 credential 或内网直连权。当前本地 monorepo 展示应用边界，没有声称 Python 对象私有属性能隔离同进程恶意代码，也未部署网络隔离或数据库角色划分。

## 本阶段刻意未实现

不实现 Hypothesis、RootCause 结论、Planner、LLM、Prompt、Agent Loop、Repair、Approval、Capability Token、Write Tool、Evaluator、自动结案、自动契约判定、业务 TTL、通用冲突推理、Event Sourcing、Kafka、Vector DB、RAG 或 UI。现阶段也不引入 LangGraph、CrewAI、AutoGen。数据库 bootstrap 为追加建表，正式结构演进的 migration framework 不在本次范围。

## 本次实测结果

2026-09-11，在项目虚拟环境运行完整测试集：

| 后端 | 命令 | 结果 |
|---|---|---|
| SQLite | `python -m pytest -q -x --tb=short -p no:cacheprovider --basetemp=.local/pytest-harness-final` | **100 passed, 1 skipped**，12.25s |
| PostgreSQL | 配置专用 `TEST_POSTGRES_URL` 后运行 `python -m pytest -q -x --tb=short --postgres -p no:cacheprovider --basetemp=.local/pytest-harness-pg-01` | **101 passed**，23.74s |

SQLite 跳过的是原有 PostgreSQL 专用 reopen 测试；PostgreSQL 执行全部测试。两次运行各有 2 条现有 Starlette/AnyIO 依赖弃用提示，没有失败。相对已有 60 项测试，本次增加 41 项测试实例，覆盖全部要求及两条 Harness HTTP 集成路径。PostgreSQL 每个 fixture 使用随机独立 schema，本次启动的本地测试实例已在测试后停止。

已实际调用生成 [S6 完整 View](examples/s6-case-evidence.json) 和 [S8 完整 View](examples/s8-case-evidence.json)，不是手写预期 JSON。S6：7 calls、25 Evidence、0 conflicts、INVESTIGATING；S8：6 calls、6 lookup Evidence、payment_finality_evidence.knowledge 为 UNKNOWN。示例中版本字段类型保持原 DTO 的 `string` / `integer` 序列化值。
