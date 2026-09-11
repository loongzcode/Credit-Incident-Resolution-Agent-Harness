# Step 9：Durable Recovery

恢复依据持久化 Case、CaseCall、Observation、Evidence、Capability 和 SideEffectLedger，重新建立可以证明的状态；不把进程异常解释为业务失败，不把恢复写成 `except: retry()`。所有演示数据和外部效果均为 synthetic fixtures，无真实银行或个人数据。

```text
Case resume
  → ReadObservationRecoveryService：按 dispatch correlation 找原 Observation
  → 原 provenance 校验 + deterministic extraction + 原子幂等发布
  → inspect unresolved effects
  → fresh Case/Evidence Snapshot → new investigation run

RecoveryScanner → SQL claim/lease
  ├─ PREPARED：原签名 Capability + fresh authorization/preflight → 唯一 dispatch CAS
  └─ DISPATCHED / UNKNOWN / ACCEPTED：status resolver.lookup → durable proof → reconcile CAS
       → APPLIED / ACCEPTED / FAILED_CONFIRMED / UNKNOWN
       → STOP；没有 Evaluator 或 CLOSED
```

## 实际文件

```text
src/credit_harness/recovery/
  models.py       类型、Policy、Lookup Contract、Proof、Result、Attempt、Audit
  tables.py       read recovery / effect recovery state / attempt / audit / checkpoint
  read.py         Orphan Observation recovery
  identity.py     从 Ledger + Intent + Capability 重建效果身份
  proof.py        关联、版本、新鲜度、external ref、结果语义校验
  repository.py   SQL claim、lease、backoff、持久 proof、CAS、Scanner
  prepared.py     唯一可恢复 dispatch 的 PREPARED 路径
  service.py      只读 Lookup Gateway 与 deterministic Coordinator
  case.py         调查重启前恢复与未决效果检查
  checkpoint.py   最小 durable Agent checkpoint，仅作审计
src/credit_harness/adapters/synthetic_effect_resolver.py
scripts/demo_recovery.py
tests/test_recovery.py
```

原有模块的改动保持集中：EvidenceRepository 提取共享事务发布方法；授权存储增加写围栏、原签名私有存储和恢复索引同步；Agent Runtime 在新 Run 前恢复 reads，逐轮保存最小 checkpoint；bootstrap 增加新表与 correlation 索引。现有 Step 8 execute 的重复请求仍只返回已有 Ledger，不自动进入恢复服务。

## Read Orphan Recovery

服务端可能已经提交 Observation，HTTP response 却丢失；CaseCall 为 ERROR 或 DISPATCHED，Evidence 尚未发布。恢复调用：

```python
ReadObservationRecoveryService(evidence_repository).recover(case_id, call_id)
```

服务从 durable Call 取得 correlation，禁止调用方提供任意 Observation。必须恰好一个 Call，零或一个 Observation；多条记录返回 AMBIGUOUS_OBSERVATION，不猜、不删除历史。NULL / 不匹配 correlation 返回 PROVENANCE_INVALID 或 OBSERVATION_NOT_FOUND。索引为非唯一，以保留并检测 legacy ambiguity。

找到记录后复用原 EvidenceRepository 校验：simulation、grant、tool、request/order、Observation ID、content hash、dispatch correlation，以及 Observation 只能绑定一个 Call。新增内部 recovery 路径允许 ERROR / DISPATCHED 完成发布；原普通 record_call 的 replay 拒绝规则保留。

Case 行 SQL 锁协调发布，Evidence、origin、Call OBSERVED 和 ReadDispatchRecoveryRow 在同一事务提交。中途 crash 全部回滚；重复或并发恢复只生成一套 Evidence。已发布返回 OBSERVATION_ALREADY_PUBLISHED，refs 与首次一致，不再推进 Case revision。首次发布继续推进 revision，使旧 Proposal 失效。预算已经在原 dispatch 预占，恢复不再次 reserve、不退款、不重发。

OBSERVATION_NOT_FOUND 仅说明当前 durable store 没找到记录。客户端 transport timeout 不产生 TIMEOUT Evidence；只有原 Observation 本身返回合法 TIMEOUT，才可以提取相应 lookup Evidence。Read Recovery 没有 Tool client，不访问 Oracle。

## PREPARED 与 DISPATCHED 的界线

PREPARED 证明尚未成功越过 durable dispatch CAS；DISPATCHED 则意味着请求可能已发出，即使本地实际 crash 在 adapter 调用前也不能重发。UNKNOWN 表示发出后结果不明确；ACCEPTED 只证明受理。后三者的恢复路径只持有 status resolver，没有副作用 adapter 或 signer。

PREPARED 恢复需要原 effect_id、idempotency_key、capability、correlation，保持 attempt_count 从 0 到 1。不会换 UUID 绕过旧 Ledger。

Step 8 prepare 自己会推进 Case.updated_at。`_PreparedCases` 只在真实当前 revision **精确等于 ledger.prepared_case_revision** 时，在只读的验证输入里还原该次自身 CAS 前的 revision，再重新运行原 Validator / Preflight，验证得到的 Intent 必须精确等于原 Intent。它不回写 Case，也不忽略随后发生的任何 Evidence、budget 或生命周期变化。最终 dispatch_prepared 再以真实 prepared revision、当前审批/TTL/版本、当前恢复 lease 和 PREPARED 状态做事务检查与 CAS。

未满足条件时保留 PREPARED，返回 BLOCKED_PREPARED 和枚举原因；不尝试重新签发或重新审批。原 Step 8 legacy Capability 没有持久化签名时，同样阻断恢复发送。

为了重启后使用**原签名**而非重新签发，Step 9 增加 Runtime 私有 `capability_signatures` 表，和 issuance payload 在同一事务保存。签名不进入 Evidence、Checkpoint、模型输入或恢复 Audit。它属于敏感授权存储，需要与整个授权数据库同等保护；此 POC 不声称已经实现 KMS、密钥轮换或完整 IAM。

## Status Resolver 与 NOT_FOUND

独立接口：

```python
resolver.lookup(correlation_id, command_identity) -> SideEffectLookupResult
```

Identity 由 durable Ledger + 已保存 AuthorizedIntent + 原 Capability 重建，校验全部 scope/hash 和原 effect key，再构建 typed command。调用 recover 时不能传其他 target/payload。历史查证不检查当前写授权是否过期或撤销。

RecoveryCapability 是**状态查询语义的静态契约**，不是业务写 Capability：

| 字段 | 意义 |
|---|---|
| resolver_id / contract_version | 已配置的可信 resolver 与契约版本 |
| supports_lookup | 是否真的有查询能力 |
| not_found_proves_no_effect | 缺失能否严格证明未生效，默认 false |
| lookup_freshness_sla_seconds | 查询结果允许的最大年龄，默认 30 秒 |
| supports_external_effect_ref | 是否支持明确的外部效果引用 |

| 查询结果 | Ledger 结果 |
|---|---|
| FOUND_APPLIED | APPLIED |
| FOUND_ACCEPTED | ACCEPTED |
| FOUND_FAILED_NO_EFFECT + no_effect_confirmed | FAILED_CONFIRMED |
| NOT_FOUND + 强契约保证不存在即未生效 | FAILED_CONFIRMED |
| NOT_FOUND + 普通契约 | UNKNOWN |
| INDETERMINATE / lookup transport error | UNKNOWN |

Synthetic Resolver 只查询 SyntheticEffectRow，不读业务 WorldState / GroundTruth。存在匹配 correlation、effect key、payload hash 的记录即可返回 FOUND_APPLIED。

本实现对 synthetic NOT_FOUND 也默认保守：事务数据库能证明“此刻没有记录”，但不能排除某个原 Worker / 原请求稍后才到达。强不存在语义在测试中由显式 Fake Resolver Contract 验证；不能把普通 MQ / partner API 或单纯 ACID 属性当成这种保证。真实集成要得到更强语义，必须有请求关闭、终结或等价的远端契约。

## Proof 与状态转换

Lookup Gateway 绑定部署配置的 resolver，调用 lookup 后才持久化严格结果与 RecoveryProof。恢复 API 只接受 effect_id，不接受外部提交的 proof、result、resolver contract 或目标参数。

Proof 绑定 effect_id、correlation_id、resolver_id、contract_version、command identity hash、lookup status、external ref、observed_at、result hash。原始 partner response、异常文本、credential、签名和 PII 不进 Audit。

`recover_transition(claim, now)` 只读取已持久化且归属于当前 lease 的 lookup/proof，重新验证其 hash、当前历史身份、配置的 resolver、关联与新鲜度后做状态 CAS。没有 durable lookup proof，不能从 UNKNOWN 得到 APPLIED / FAILED_CONFIRMED。普通 Step 8 transition 仍禁止 UNKNOWN → APPLIED。

已绑定 external_effect_ref=A 后查询得到 B，会产生 LOOKUP_PROOF_CONFLICT，保留 A 并要求升级；不会覆盖。查不到一个已有 external ref 的操作也不能据此抹去过去的受理/效果记录。原 Worker 若先提交最终 receipt，恢复 Worker 的 CAS 不覆盖它，返回最新 Ledger 和 NOTHING_TO_DO。

允许 UNKNOWN → ACCEPTED / APPLIED / FAILED_CONFIRMED / UNKNOWN，但它永远不能回 PREPARED 或 DISPATCHED。ACCEPTED 同样只允许查证后推进或保持不确定。APPLIED / FAILED_CONFIRMED / NOOP / REJECTED 默认无恢复工作；VERIFIED 保留给未来 Evaluator。

## Lease、并发和 backoff

`effect_recovery_state` 显式保存 tenant、case、status、ledger_updated_at、next_eligible_at、lease owner/until/token、attempt count 和 escalation flag。与每次 Ledger mutation 同事务同步；有 `(tenant_id, status, next_eligible_at)` 索引。Scanner 查询到期、过 grace、无有效 lease 且未超限的记录，默认最多 100 条，不作业务判定。

默认 RecoveryPolicy：grace=30 秒、lease=60 秒、max_attempts=3、backoff=(30,120,600) 秒。可通过 typed constructor 配置，测试缩短时间并使用受控时钟。记录 next_eligible_at，不 sleep、不部署后台 scheduler。尚在正常处理窗口的 DISPATCHED 不立即被判为 crash。

claim 的 SQL 事务先锁 tenant-scoped Case，再锁/检查恢复状态；同一 lease 内最多一个 Worker 查询。每次 claim 使用新的 fencing token，并追加独立 EffectRecoveryAttemptRow。lease 过期后另一个 Worker 可以重新执行 **只读 lookup**，旧 Worker 的 proof/transition 会因 lease token/expiry 失效而被拒绝。

PREPARED 的最终 dispatch CAS 同时检查 lease token，防止过期 Worker 恢复发送。普通 Worker 与恢复 Worker 竞争时仍只有一个能完成 PREPARED → DISPATCHED。SQLite 依靠数据库写锁/条件更新，PostgreSQL 使用行锁/条件更新，不依赖 Python Lock。

EffectRecoveryAttempt 保存 recovery_id、worker_id、effect_id、sequence、开始/结束时间、source/result status、resolver、typed lookup_result、proof hash、failure code。恢复查询五次也不会把 SideEffectLedger.attempt_count 变为五；它始终是实际 dispatch 次数，最大 1。过期 lease 遗留的未完成 attempt 会标记 LEASE_LOST。

连续无法确认达到上限后，requires_escalation=true，Scanner 不再选取；Ledger 仍 UNKNOWN，升级标记不自动改变 Case 生命周期。失败原因仅为枚举，例如 LOOKUP_TIMEOUT、LOOKUP_TRANSPORT_ERROR、LOOKUP_PROOF_CONFLICT、RECOVERY_AUTHORIZATION_EXPIRED、RECOVERY_STALE、RECOVERY_LIMIT_REACHED。

Lease 只协调 Worker，既不是 Approval actor，也不是业务 Capability。它不会扩大动作权限。

## 到期、撤销与策略升级

| 变化 | PREPARED：首次发送尚未线性化 | DISPATCHED / UNKNOWN / ACCEPTED |
|---|---|---|
| Capability 到期 | 阻断 resume | 允许只读 status lookup |
| Approval 到期/撤销 | 阻断 resume | 允许只读 status lookup |
| Remediation policy/catalog 或 authorization policy 升级 | 阻断旧授权 resume | 使用原持久化身份继续查证 |
| 新 Evidence / Case revision | fresh preflight / CAS 拒绝 | 不妨碍查询既有操作 |

撤销只能阻止尚未越过发送界线的动作，无法撤回一个可能已经发出的请求。恢复事实不需要重新授予该业务动作的权限。

## Write Fence 与调查重启

issue_capability 在与签发相同的 Case 锁事务里查询 Ledger：同一个 Case + effect identity 若处于 DISPATCHED、UNKNOWN 或 ACCEPTED，拒绝 `WRITE_FENCED_BY_UNRESOLVED_EFFECT`。新 Intent ID、审批或新模型说明不能绕过该围栏。不同 effect identity 可按原策略处理，例如创建人工复核任务。保留原 Step 8 的更后置幂等防线。

CaseRecoveryCoordinator 在新 investigation Run 前先补齐 pending reads，再检查未决效果；它不隐式调用写恢复或业务 Read Tool。读调查可以继续，新的同范围写授权仍被 hard fence 阻断。

Agent Runtime 每次 run 都产生新 run_id。DurableAgentCheckpoint 保存 run_id、case_id、last_completed_turn、last_snapshot_id、last_decision_id、last_call_id、stop_status、updated_at。逐轮和结束时写入，供审计/debug 使用；不保存模型 conversation、private reasoning 或 raw context。

checkpoint 不是执行凭据。若 Tool/Evidence 已提交但 checkpoint 未更新，重启仍以 durable Case/Call/Evidence 为准。旧 Planner Decision 不会继续执行，旧 Remediation Model 也不会被询问“是否再 replay”。

## 六个崩溃点

| 点 | 崩溃位置 | 持久状态 | 新 Worker 行为 |
|---|---|---|---|
| A | Capability issued，PREPARED 之前 | 没有 Effect Ledger | Scanner 无工作；新的显式 execution request 仍需 fresh validation |
| B | PREPARED 提交，DISPATCHED 之前 | PREPARED | 原授权、原身份、原 correlation 下重新预检并尝试唯一首次发送 |
| C | DISPATCHED 提交，adapter 调用之前 | DISPATCHED | 只 lookup；普通 NOT_FOUND 保持 UNKNOWN |
| D | adapter applied，response 丢失 | DISPATCHED / UNKNOWN | lookup matching effect → APPLIED，不重发 |
| E | 收到 receipt，本地 APPLIED 尚未提交 | DISPATCHED | 忽略丢失的内存 receipt；lookup → APPLIED |
| F | APPLIED 已提交，调用方未收到 | APPLIED | 返回已有结果，无恢复 dispatch |

数据库与外部 dispatch 之间仍然没有 exactly-once 分布式事务。Step 9 通过持久状态、单次发送界线和只读查证控制不确定性，不能把 grace、lease 或一个索引查询包装成全局 exactly-once。

## 演示与真实数据

```powershell
# read-orphan 不需要签名密钥；其他场景使用本进程的 synthetic demo 密钥。
$env:CAPABILITY_SIGNING_SECRET = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
.\.venv\Scripts\python scripts/demo_recovery.py --scenario read-orphan
.\.venv\Scripts\python scripts/demo_recovery.py --scenario effect-timeout
.\.venv\Scripts\python scripts/demo_recovery.py --scenario worker-crash-after-prepared
.\.venv\Scripts\python scripts/demo_recovery.py --scenario worker-crash-after-dispatch
# --output .local/recovery.json 可保存完整结构
```

| 实际导出 | 含义 |
|---|---|
| [read-orphan-recovery.json](examples/read-orphan-recovery.json) | 服务端 Observation 已提交，客户端超时；新 Worker 发布原 Evidence，Budget 不变，只 dispatch 一次 |
| [s6-effect-timeout-recovery.json](examples/s6-effect-timeout-recovery.json) | Message 已 CONSUMED，响应丢失，UNKNOWN 经匹配的 status proof 恢复为 APPLIED |
| [prepared-recovery.json](examples/prepared-recovery.json) | PREPARED 后崩溃，沿用原 effect/correlation，首次 dispatch 完成 |
| [dispatched-recovery.json](examples/dispatched-recovery.json) | DISPATCHED 后、adapter 前崩溃，NOT_FOUND 按保守契约保持 UNKNOWN，绝不补发 |

副作用恢复演示前后 Evidence 完全相同，Case 仍 INVESTIGATING。S6 集成测试在 Recovery APPLIED 后另行显式调用 MESSAGES Read，才产生新的 CONSUMED Evidence。Recovery 服务本身没有这次 Read 的能力。

本次实际导出摘要：

| 场景 | 恢复前 → 后 | dispatch attempt_count | Evidence / Budget |
|---|---|---|---|
| read-orphan | ERROR → OBSERVED；再次恢复 ALREADY_PUBLISHED | Tool 总计 1 次 | Evidence 0 → 6，used_tool_calls 1 → 1 |
| effect-timeout | UNKNOWN → APPLIED；FOUND_APPLIED proof | 1 → 1 | Evidence 37 → 37 |
| worker-crash-after-prepared | PREPARED → APPLIED | 0 → 1，原 effect/correlation | Evidence 37 → 37 |
| worker-crash-after-dispatch | DISPATCHED → UNKNOWN；普通 NOT_FOUND | 1 → 1，不补发 | Evidence 37 → 37 |

四份导出均重新通过 Pydantic 校验；lookup result hash 与 audit proof / attempt proof_hash 一致。Read orphan 的 6 条 Evidence 全部引用同一原 Observation，重复恢复返回相同 Evidence IDs。所有场景的 Case 均为 INVESTIGATING。

## 验证与阶段边界

Step 9 新增 82 个 Recovery 测试实例，总计 842 个。最终冻结代码后实测：

| 验证 | 结果 | 耗时 |
|---|---|---|
| Recovery + Side Effect + Agent Runtime 专项 | **210 passed**（82 + 61 + 67） | 251.80 秒 |
| SQLite Recovery 并发单独运行 | **6 passed, 76 deselected** | 10.11 秒 |
| PostgreSQL Recovery 并发单独运行 | **6 passed, 76 deselected** | 12.76 秒 |
| SQLite 全量 | **839 passed, 3 skipped** | 565.51 秒 |
| PostgreSQL 全量 | **840 passed, 2 skipped** | 624.55 秒 |

命令为 `python -m pytest -q --tb=short -p no:cacheprovider`；PostgreSQL 配置专用 `TEST_POSTGRES_URL` 并追加 `--postgres`。专项指定 `tests/test_recovery.py tests/test_side_effect.py tests/test_agent_runtime.py`。并发单独指定 `tests/test_recovery.py -k "two_workers or lease_can_be_taken or transition_is_cas_safe or expired_lease"`；所有运行使用独立 `--basetemp`，PostgreSQL 每个 fixture 使用随机隔离 schema。

两库均跳过两个可选在线 LLM 测试；SQLite 额外跳过 PostgreSQL 专用 reopen 测试。没有外网模型请求，Provider 离线 Structured Outputs 测试在全量中执行。各运行仅有一条既有 Starlette/AnyIO 弃用提示。`compileall`、四份 Demo schema/proof hash 验证与 `git diff --check` 均通过。

既有迁移测试只调整了 fixture：先删除新 correlation 索引，再删除列以模拟旧 schema；所有 legacy 数据保留与 provenance 断言不变。其余既有测试语义未修改。

静态测试禁止 recovery package 依赖 openai、planner.model、remediation.model、simulator、WorldState、GroundTruth 或 Evaluator。只有 prepared.py 可调用受控 dispatch 前置入口；lookup 路径没有 adapter.dispatch。Synthetic resolver 仅读操作台账，不读 Oracle。Recovery Audit 不保存 exception 原文、token、raw partner response 或 PII。

未实现 Independent Evaluator、业务 VERIFIED / CLOSED、Skill/Experience Memory、System Registry、UI-1、Money Movement、通用 Workflow/Saga、真实 MQ/银行集成、后台 scheduler 或无限恢复。现有进程内模块和应用 API 边界不等于完整企业 IAM，也不声称防御持有任意 Python/数据库管理权限的攻击者。

APPLIED 只表示指定局部效果已被确认，仍需 Read Tool → Observation → Evidence，再由未来独立 Evaluator 判断三方业务是否收敛。恢复次数多、查到了 effect、或新 checkpoint 写入都不能让 Case 自动关闭。
