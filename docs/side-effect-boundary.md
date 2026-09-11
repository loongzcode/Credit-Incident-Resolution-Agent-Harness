# Step 8：Authorization + Capability + Idempotent Side-Effect Boundary

本阶段首次执行 **synthetic 非资金副作用**。没有真实金融连接、真实个人信息、资金客户端、自动恢复或 Evaluator。Step 7 的 RemediationPlanner 仍停在 PROPOSED Intent；Step 6 的只读调查循环没有接上执行器。

```text
Step 7 RemediationIntent (PROPOSED)
  → fresh deterministic validation / preflight
  → durable Approval (L2)
  → signed, registered, short-lived Capability
  → fresh execution validation / command binding
  → SQL transaction: approval + TTL + capability consumption + Case revision CAS
  → Ledger PREPARED (commit)
  → final approval / TTL / revision check → DISPATCHED (commit)
  → typed synthetic adapter dispatch
  → ACCEPTED / APPLIED / UNKNOWN / FAILED_CONFIRMED
  → STOP

独立后续 Read Tool → Observation → Evidence
业务验收、VERIFIED、Case CLOSED：尚未实现
```

## 代码与运行

```text
src/credit_harness/authorization/
  models.py       Approval / Capability / Ledger / Receipt / audit schema
  signing.py      CapabilitySigner Protocol + environment-backed HMAC
  commands.py     four typed commands + deterministic target/payload binding
  tables.py       five durable authorization tables
  store.py        tenant-scoped approval, issuance, CAS, idempotency, audit
  service.py      fresh verifier, authorization service, execution service
src/credit_harness/adapters/synthetic_remediation.py
                  privileged synthetic external adapter + effect/task tables
scripts/demo_side_effect.py
tests/test_side_effect.py
```

授权域通过 adapter Protocol 依赖外部系统，没有 SimulatorAdmin / WorldState / GroundTruth，也没有模型或 Tool Client。SQL 表复用现有 Base。只有明确隔离的 synthetic adapter 能读取并更新模拟世界；它不返回 Oracle。测试及 demo 的 provisioning 区域可安装显式 synthetic READ fixture。

`create_authorization_schema` 与 `create_synthetic_effect_schema` 为新表提供独立本地初始化入口。无需修改现有 Simulator 场景、Read API、Evidence schema 或 Agent API。新增服务是可信应用内部 API；没有将签发、审批、执行暴露给 Agent 的 FastAPI 路由。

```powershell
# 本地 demo：在当前进程生成独立随机 secret；不提交、不打印。
# 持久服务应由自己的 secret 配置提供稳定密钥。
$env:CAPABILITY_SIGNING_SECRET = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
$env:CAPABILITY_TTL_SECONDS = '300'
.\.venv\Scripts\python scripts/demo_side_effect.py --output .local/s6-side-effect.json
.\.venv\Scripts\python scripts/demo_side_effect.py --scenario S7 --output .local/s7-side-effect.json
.\.venv\Scripts\python scripts/demo_side_effect.py --scenario S7 --administrative
.\.venv\Scripts\python scripts/demo_side_effect.py --reject
.\.venv\Scripts\python scripts/demo_side_effect.py --timeout
```

Demo S6-ready 在正常 Observation 持久化、hash 和 Evidence extraction **之前**提供一份明确的当前部署兼容性观测；不修改普通 S6，不推断 Consumer 原来使用哪个版本。普通 S6 当前部署未知时，原 Step 7 preflight 继续阻断 Replay。演示使用 Fake 模型提出候选，经真实 Step 7 校验、选择，然后由可信 bootstrap 发起审批和执行。没有 LLM 自动执行路径。

## Approval 与 Capability 分离

ApprovalRequest 保存完整不可变 RemediationIntent，因此同时绑定 Case / tenant / order、action、target、Evidence refs/hash、snapshot、preflight 和 policy/catalog version。状态为 PENDING、APPROVED、REJECTED、EXPIRED、REVOKED；decision 保存 actor_ref、明确 Enum reason、decided_at。拒绝不会签发 capability 或创建 effect。到期由 durable `expires_at` 与当前时间计算 EXPIRED 视图，不需要后台 timer；不会悄悄恢复到 APPROVED。

SQLApprovalStore 每次重新读取审批，不缓存批准结果。L2 Replay/Redelivery 必须有当前有效批准；L1 task/review 不要求 human approval，但仍需 capability。NO_REMEDIATION 在执行入口生成带审计的 NOOP，不 dispatch，attempt_count=0，也不改变 Case revision。

这不是完整 IAM：actor_ref 由未来可信的人工作业入口绑定认证主体，当前 demo 为 synthetic `OPERATOR-001`。模型不提供 actor，不审批自己。

ExecutionCapability 包含：

| 类别 | 字段 |
|---|---|
| 身份与范围 | capability_id、intent_id、tenant_id、case_id、internal_order_id、action_type |
| 不可替换绑定 | target_hash、payload_hash、snapshot_id、evidence_hash、preflight_fingerprint |
| 授权前提 | approval_id、policy_version、catalog_version、authorization_policy_version、expected_case_revision |
| 生命周期 | issued_at、expires_at、max_effects=1 |

HMAC-SHA256 覆盖 canonical JSON 完整 payload；验证使用 `hmac.compare_digest`。环境变量 `CAPABILITY_SIGNING_SECRET` 至少 32 字节，禁止代码默认 key；测试 key 均明显 synthetic。TTL 默认 300 秒，配置只允许 1–300 秒，approval 默认 300 秒。数据库保存已签发 payload 和 used_effect_id，不保存 secret 或签名 token；一个签名正确但未注册、或与已注册 payload 不符的 token 仍被拒绝。

Capability 是 Runtime 授权域的对象；它的签名、完整 token、approval actor 均不进入模型输入。Demo 只打印最小范围/TTL 摘要，Audit 保留链路 ID、不保留签名。Approval 是人的决定，Capability 是受限执行凭据；两者都不证明业务成功。

## Fresh authorization 与最小权限

签发前和首次执行前，FreshIntentVerifier 都从 Case + durable Evidence 重建当前 state，重新运行原 Validator / Preflight。检查 content-addressed Intent、完整 target、当前 snapshot、Evidence 和 policy/catalog、Payment Identity、L2 safety gaps、当前部署 readiness、already-consumed/delivered 等。重新得到的 Intent 必须精确等于批准对象。模型的 reason_summary 不参与任何判断。

CommandBuilder 只接受这个重新验证的 Intent 和已验签 Capability，重新计算 target_hash 和 typed command 的 canonical payload_hash。执行地址只来自 Step 7 的 durable RemediationTarget，不解析模型说明，不使用 EXTREF alias，不接受 URL、SQL、shell、headers、credential 或 arbitrary payload dict。

| Typed Command | 唯一允许的 synthetic 效果 |
|---|---|
| ReplayCallbackCommand | 精确匹配既有 failed message + callback event；同一条消息变为 CONSUMED |
| RedeliverAssetNotificationCommand | 精确匹配既有 failed delivery；该 delivery 变为 DELIVERED |
| CreateReconciliationTaskCommand | 创建一条 durable synthetic task |
| RequestOperatorReviewCommand | 创建一条 durable synthetic review task |

L3/L4、MONEY / ORDER_STATE / BULK scope 在授权前拒绝，无资金 Command Builder，无新放款、支付、扣款、代偿、退款或改金额/账户入口。Replay 不更新 Guarantee / Accounting / Asset loan status；Redelivery 不修改支付。当前修复的是被许可的局部效果，绝不顺手把三方状态改成一致。

## 原子 prepare、并发与 one-effect

Case.updated_at 是已有 revision。已有读取预算预留、Evidence 写入及 Case pause 都会更新它。执行准备使用相同的 Case 行协调点：

1. 按 tenant + Case 获取 SQL 写锁；统一锁顺序为 Case → approval/capability/effect。
2. 在事务内读取有效审批、TTL、当前 policy 和已注册 capability payload。
3. 查唯一 idempotency key；相同 payload 返回原 Ledger，不再次执行。
4. 检查 capability.used_effect_id。已绑定其他 effect 立即拒绝。
5. CAS 比较 expected_case_revision，仅推进 revision，不更改 Case status 或 read budget。
6. 原子写入 PREPARED、consume capability、Audit；提交后才可继续。
7. PREPARED → DISPATCHED 再检查审批、TTL、prepared_case_revision，并用状态 CAS 取得唯一 dispatch 权。普通 transition 方法无法绕过该检查。

SQLite 由 Case UPDATE / 数据库写锁串行化；PostgreSQL 由同一行锁和条件 UPDATE 串行化。不依赖 Python mutex、进程内 bool 或单 Worker 假设。SQL unique key 和 capability usage 同时约束效果。两个独立 service/repository 同时执行同一个 token，或两个 token 对应同一 effect，至多一个获得 dispatch；另一个返回已有 PREPARED / DISPATCHED / APPLIED 等记录。

fresh rebuild 在事务外完成，但事务内的 Case CAS 将它绑定到未变化的 Evidence/Case revision；preflight 后新增 Evidence 会使 CAS 失败。prepare 后撤销/到期或新增 Evidence，会在 final dispatch gate 阻断。已提交 DISPATCHED 是最终授权线性化点：此后撤销不能撤回一个已发出的外部请求。本阶段不声称跨数据库与远端网络的分布式原子性。

未来真实 adapter 还需要目标系统自身的版本检查与幂等契约。当前 synthetic adapter 在模拟系统事务内重新检查目标仍存在且失败，再只更新该目标。

## Idempotency 与 Ledger

`effect_id = idempotency_key = SHA256(tenant, case, order, action, target_hash)`，采用 canonical JSON。特意不把新 snapshot / intent ID / rationale 放进 effect identity：一次重新调查或重新签发不应给同一效果换名字。payload_hash 单独绑定并比较；同 key 不同 payload → IDEMPOTENCY_CONFLICT。capability_id 和 dispatch_correlation_id 是不同的随机执行身份，不参与业务 effect key。

这是第一版“同一个 Case 对同一个目标动作至多一次”的保守范围；L1 task 也遵循该范围。后续若允许某目标再次修复，需要显式的新业务操作版本，不能自动换 UUID 绕过旧 Ledger。synthetic 外部系统另外以 simulation + action + target 去重，防止不同 Case 重复修改同一条 message/delivery；跨 Case 不同 payload 返回明确未执行拒绝。

Ledger 持久化 intent/approval/capability/effect/correlation 链、scope、snapshot/evidence/target/payload hash、状态、attempt_count、时间和 external_effect_ref/failure_code。Audit 使用数据库 sequence 保证即使逻辑时间相同也可按发生顺序检查链路，接口只追加。

允许的状态变化：

```text
PREPARED → DISPATCHED → ACCEPTED / APPLIED / UNKNOWN / FAILED_CONFIRMED
ACCEPTED → APPLIED / UNKNOWN / FAILED_CONFIRMED
NOOP: terminal
APPLIED: Step 8 terminal
UNKNOWN: Step 8 terminal, no retry
REJECTED: reserved pre-dispatch terminal; unauthorized calls currently return a
          typed AuthorizationError without creating a dispatchable Ledger
VERIFIED: reserved, no Step 8 transition
```

ACCEPTED 只代表受理；APPLIED 只代表 typed adapter 回执确认这个局部 command 的效果；VERIFIED 要求未来独立业务验收。需要明确结果的 Ledger transition 必须带同一 correlation、同 outcome 的 typed receipt。FAILED_CONFIRMED 还必须声明 no_effect_confirmed=true，不能用 HTTP 状态或异常猜测未执行。

Transport timeout、provider error、非法回执或 correlation 不匹配均产生 UNKNOWN。即使 synthetic adapter 已经完成消息消费、随后响应丢失，也保持 UNKNOWN。原始异常文本不进入日志/Audit/Evidence。

再次调用同一效果时，已有 PREPARED、DISPATCHED、UNKNOWN 只返回已有记录，永不自动 dispatch；APPLIED 返回幂等结果。重复调用仍验证有效签名、审批、TTL 和 policy，过期 token 会被拒绝；可信检查入口可继续读取原 Ledger。进程崩溃留下 PREPARED 或 DISPATCHED，catchable 不确定错误留下 UNKNOWN，attempt_count 最大为 1。

## 为什么执行后必须重新 Read

ExecutionService 没有 Evidence writer，也不调用 Read Tool。Receipt 是外部 command 的回执，不是资金事实或 Case outcome。S6-ready 执行前后的 Evidence 完全相同；显式再次调用原 MESSAGES Tool 后，ObservationService 持久化新 Observation，EvidenceExtractor 才生成 `MESSAGE_CONSUME_STATUS=CONSUMED`。原 FAILED Evidence 保留为历史。

S7 同理通过 ASSET_DELIVERY Read 产生新 DELIVERED Evidence。执行不消耗 read budget；后续 Read 正常消耗。无论 Ledger APPLIED 或后续局部读成功，Case 都不 CLOSED，RemediationIntent 仍为 PROPOSED 的不可变提案记录，授权与执行结果保存在各自域。

## 明确保留给后续阶段

没有 Durable Recovery、orphan reconciliation、UNKNOWN 查询/重试、自动恢复循环、独立 Evaluator、VERIFIED / CLOSED、UI-1、真实 money movement。没有 OAuth、完整 IAM/RBAC/ABAC、HSM/KMS、密钥轮换/分发服务、银行 tokenization 或真实 MQ/银行客户端。所有身份、任务与资金数据均为 synthetic fixtures。

本阶段已建立可运行的持久化授权和非资金副作用边界；没有宣称跨系统 exactly-once，也没有把一次 APPLIED 当作金融三方业务已经收敛。

## 验收记录

现有 699 个测试实例保持不变；新增 61 个 Step 8 测试实例。Step 7.1 的历史记录保留在 remediation-boundary.md。

| 检查 | 本次结果 |
|---|---|
| SQLite full suite | 757 passed / 3 skipped，269.46 秒 |
| PostgreSQL full suite | 758 passed / 2 skipped，373.53 秒 |
| SQLite 独立 Worker 并发专项 | 2 passed / 59 deselected，3.60 秒 |
| PostgreSQL 独立 Worker 并发专项 | 2 passed / 59 deselected，4.00 秒 |
| Planner + Remediation Provider | 19 passed / 2 optional live skipped，7.47 秒 |
| 五份实际 Demo JSON | S6、S7、L1、审批拒绝、响应丢失均已运行生成 |

SQLite 跳过一个 PG 专项和两个可选在线模型测试；Provider MockTransport roundtrip 实际执行，无在线 LLM 调用。测试使用 `--postgres` 时每项创建独立 PostgreSQL schema；并发测试运行两个独立 service/repository 实例，分别验证同 token 与双 token 同 effect。只有现有 Starlette / AnyIO 弃用警告。

```powershell
.\.venv\Scripts\python -m pytest -q --tb=short -p no:cacheprovider --basetemp=.local/step8-sqlite-full
# TEST_POSTGRES_URL 指向专用本地测试库。
.\.venv\Scripts\python -m pytest --postgres -q --tb=short -p no:cacheprovider --basetemp=.local/step8-postgres-full
.\.venv\Scripts\python -m pytest tests/test_side_effect.py -k concurrent -q
.\.venv\Scripts\python -m pytest tests/test_side_effect.py --postgres -k concurrent -q
.\.venv\Scripts\python -m pytest tests/test_planner_provider.py tests/test_remediation_provider.py -q
```

实际输出摘要（完整结构、真实 hash / Evidence IDs / provenance 见 examples 文件）：

| Fixture | 效果 | Ledger | Evidence：执行前 → 执行后 → Read 后 | 新 Read 事实 | Case |
|---|---|---|---|---|---|
| [S6-ready](examples/s6-side-effect.json) | Replay existing message | APPLIED，attempt=1 | 37 → 37 → 41 | CONSUMED | INVESTIGATING |
| [S7](examples/s7-side-effect.json) | Redeliver existing delivery | APPLIED，attempt=1 | 30 → 30 → 32 | DELIVERED | INVESTIGATING |
| [L1](examples/l1-side-effect.json) | Create reconciliation task | APPLIED，attempt=1 | 30 → 30 → 30 | 未自动查询 | INVESTIGATING |
| [S6 response lost](examples/s6-side-effect-unknown.json) | Effect applied remotely, response lost | UNKNOWN，attempt=1 | 37 → 37 → 41 | CONSUMED | INVESTIGATING |
| [S6 rejected](examples/s6-side-effect-rejected.json) | Approval REJECTED | 不签发 / 不 dispatch | 不新增 Evidence | 未查询 | INVESTIGATING |

第四行特别证明：后续 READ 虽然观察到 CONSUMED，Step 8 仍不会自行把 UNKNOWN Ledger 改成 APPLIED / VERIFIED；这需要后续明确的 reconciliation / Evaluator 契约。所有成功执行示例的第二次请求都返回同一 Ledger，executed_now=false、idempotent_replay=true。
