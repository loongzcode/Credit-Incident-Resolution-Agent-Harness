# Step 13 — Durable Case Orchestration

当前补丁：**Step 13.2 — Finality Handoff Routing Integrity**。在 Step 13.1 原子恢复与 backoff 基础上，修正 requirement 路由及 Recovery／Investigation no-progress 计数隔离。

本阶段把 Investigation、Recovery 和 Independent Evaluation 之间的交接写入数据库。WAIT、ESCALATE、Effect APPLIED 都不表示业务已经成功。所有数据仍为 synthetic fixtures；不接真实用户、金融系统或在线模型。

## Work Item 与事务边界

`orchestration/models.py` 定义冻结、extra-forbid 的 `CaseWorkItem`、`WorkClaim`、`ResolutionSignal`、`CaseOrchestrationState`。工作类型为 `INVESTIGATION_RESUME / VERIFICATION_REQUIRED / RECOVERY_RECHECK / OPERATOR_FOLLOWUP`。

Work Item 包含：

| 字段 | 用途 |
| --- | --- |
| work_item_id, tenant_id, case_id | 确定性工作键与租户、Case 隔离 |
| work_type, status, reason_code, trigger | 有限类型、状态与触发原因 |
| source_ref, snapshot_ref, requirement | 原决策、Effect 或 Report 的引用 |
| expected_case_status, expected_case_revision | 创建时绑定的 Case 状态及单调 updated_at revision |
| created_at, not_before, completed_at | 显式业务时间；到期不代表已执行 |
| claimed_by, lease_token, lease_until, attempt_count | Worker 所有权与租约 fence |
| required_signal, signal_id | 必须匹配的 typed resolution trigger |
| previous_run_id, run_id, resumed_at, started_at, resume_lease_token | 跨 Run lineage 与启动防重 |
| before/after evidence/progress fingerprint, verification_requirements | 跨 Run 调查进度审计 |

工作键由 tenant、case、type、reason、source、requirement、snapshot 哈希产生。相同来源的重复交接复用同一个工作项，不靠随机 ID 去重。不同报告／决策保留独立交接记录。数据库保存 Work、Case counters、Signal、Audit、ResumedRun 和 Evaluation handoff outbox；无需 Kafka、Event Sourcing 框架或通用工作流引擎。

Agent 接受 WAIT/ESCALATE 后，`CaseRepository.pause_if_current` 在同一事务内检查原 Snapshot 执行前提、更新 Case 状态／revision，并调用 `pause_handoff` 创建 Work。创建 Work 失败则整个事务回滚，不能留下 WAITING 且没有定时任务的半成品。

## WAIT、ESCALATE 与 Signal

WAIT 创建 `not_before = now + suggested_wait_seconds` 的 timer，时间范围沿用 Planner 的 30～3600 秒。`poll_due_work(now, limit)` 只做有限扫描和 READY 更新，`tick(worker_id)` 最多处理一个已 claim 的工作；没有后台线程、sleep、cron 或无界循环。

等待不是终态真值。时间经过不创建 Observation、Evidence 或任何支付结论。恢复后仍须真实 Tool Observation。`UNKNOWN != FAILED`，重复 Timeout 不能升级为 NOT_EXECUTED。

ESCALATE 必须匹配 `OPERATOR_ACKNOWLEDGED / CAPABILITY_AVAILABLE / DEPLOYMENT_STATE_UPDATED / SOURCE_RECOVERED` 中要求的信号。单纯到期不能唤醒。Signal 绑定 tenant、case、work、order subject、类型、actor 和时间；错误类型、其他 Case、其他 tenant、过早／未来时间、陈旧 revision、已终态 Case 均拒绝。同一个 signal ID 不允许变更内容或绑定到其他工作。

Signal 只进入 orchestration 私有表。`submit_signal` 是可信基础设施接口，不注册为 Agent Tool，不接受模型自述作为来源证明。本阶段没有实现外部身份认证服务；未来接入方须先认证 actor 和 source，再提交这个对象。

**Signal != Evidence。** 信号不能满足 deployment、payment 或任何业务 Evidence Gap，也不会改变 Tool Capability Catalog。测试对提交前后的 Evidence 集合和 Gap 做等值断言。收到 deployment updated 也不能伪造“当前 Consumer 确实部署了 v2.3”。

## Lease、Resume CAS 与崩溃

所有写操作采用统一锁顺序 Case → Work。SQLite 的事务 UPDATE 锁、PostgreSQL 的 Case row UPDATE 锁共同实现相同语义：同一 Case 同时最多一个 CLAIMED 工作，同一个工作只有一个有效 lease token。租约默认 120 秒，可续租；旧 token 不能完成任务，也不能继续预约 Tool 或提交 WAIT/ESCALATE。

`CaseResumeService.resume(WorkClaim)` 不提供 `resume(case_id)`。Step 13.1 调整为：先在锁内绑定原 Case 状态／revision 和 Work claim，再执行 lease-aware read recovery 与最多一次 Effect Recovery，最后重新核验并 Resume CAS：

1. 仍拥有未过期 lease；Case 尚未 CLOSED/CLOSED_VERIFIED。
2. 当前 Case status/revision 与 Work 预期值完全一致；只有本 claim 的原子读恢复允许受控推进 expected revision。
3. not_before 到期，所需 Signal 已匹配。
4. 独立 resume／verification／no-progress 配额允许继续；PREPARED 不得被跳过。

通过后才 CAS 到 INVESTIGATING，并在同一事务记录新 run_id、resume 时间和 lease token。`start_once` 记录启动 fence。两名 Worker 重放同一 claim 时仅一个能启动 Run。

租约丢失后，新 Worker 先恢复持久化读取和 Effect。若该工作已经记录 resume/start，不能假定原 Worker 尚未调用模型／Tool；将其 BLOCKED 并产生 `INTERRUPTED_RUN` 人工跟进，禁止盲目第二次启动。这是保守的恢复策略，不承诺模型推理 exactly-once。Tool 预约后的异常仍沿用已有 dispatch correlation、Observation persistence 和 read recovery；未知结果不补造 Evidence。

外部 Tool transport 或进程崩溃若打断工作，CLAIMED 记录保留到 lease 过期。恢复任务不会重复授权 L2，也不会恢复过期 Capability。原 PREPARED Effect 只能交给已有 `PreparedEffectResumer`，继续校验原授权与现有 write fence。

## 新 Run 与新 Snapshot

恢复创建新 `AgentRunResult`，lineage 包含 `parent_run_id / resumed_from_work_item_id / resume_reason / resume_trigger`。完成的恢复 Run trace 单独持久化；旧 Run 不被追加、覆盖或当作对话历史输入。Planner 只收到从当前 Case + Evidence 重新组装的 Snapshot，没有恢复旧 chat、summary 或 draft。

Case 增加可信 `lookup_retry_after`。只有合法 durable resume 设置这个 retry window；新建 Case 禁止自行填入。Context 保留全部历史 Observation/Evidence 引用，只从这个边界重新计算当前连续失败次数。因此一次合法 WAIT／ResolutionSignal 后可以重新查源，同时当前窗口内 repeated-query hard policy 继续有效。Context schema/policy 升为 5，compaction 升为 4；模型不能修改该窗口。

## 三类交接

**SideEffect → Verification：** Ledger 状态变更的原有 `synchronize_effect` 同事务调用 `effect_handoff`。APPLIED 的 Callback Replay / Asset Redelivery 生成对应 post-effect verification requirement；不在 SideEffectExecutor 内执行 read Tool。DISPATCHED/UNKNOWN 生成延迟 Recovery recheck；FAILED_CONFIRMED 生成 operator followup。行政类 APPLIED 也不能当成贷款已恢复。

**Recovery → Verification：** Recovery 更新 Ledger 使用同一个 handoff。正常 APPLIED 与 recovered APPLIED 使用相同 effect source key，重复事件只能产生一条对应 Work。重启 bootstrap 为旧 Ledger 补建交接，同时保留已有 lease、attempt、backoff；不补签旧 Capability。

**Evaluator → Work：** `IndependentEvaluator` 保持只读。`EvaluationRepository.record` 把 Report 与待交接 outbox 同事务保存。`EvaluationHandoffService` 只消费已持久化且正确绑定的 Report：

| Verdict | 交接 |
| --- | --- |
| PASS | 仅调用现有 VerifiedClosureService，由它再次验证 fresh report 并 CAS 关闭 |
| FAIL | Operator followup；不自动再修复 |
| INCONCLUSIVE | 由 RequirementRouter 把缺口路由到 Read Verification、Effect Recovery 或 Operator Followup |

outbox 有租户／状态／sequence 索引，有限扫描。工作创建完成但 ack 前崩溃可幂等重放；陈旧报告被取消，不阻塞后续报告。没有将 Report 或 Signal 写成 Evidence。

验证 Worker 的静态映射仅覆盖已有 PAYMENT、GUARANTEE、FUND、ASSET、ACCOUNTING、MESSAGES、ASSET_DELIVERY 能提供的指定 requirement。每一轮只读取同一报告中的可用、去重 Tool 集合，不扫 ALL_TOOLS；未知 requirement 留给人工。每次读取都重新组装 Snapshot，经 CaseToolExecutor、租约 fence、原 order/tool scope 和原 Tool Budget。批次中耗尽 Tool 预算会停止并升级。有限批次读完后再评估，避免对一半刷新、一半陈旧的状态过早判断。

## 独立配额、进度与 Terminal Fence

默认 `max_resume_cycles=5 / max_verification_cycles=5 / max_no_progress_cycles=3`；每项有模型上限。Demo 在 Case 开始前配置 resume=3、verification=5、Tool Budget=24。开始恢复后不能重设 cycles；也不会增加原 Tool Budget。

跨 Run fingerprint v2 同时覆盖真实 Evidence、Effect 语义状态／身份和未决 verification requirements。仅 Case revision、状态切换、Ledger 时间、Recovery attempts 或 budget 使用量变化不能算进展。新 Tool Timeout 是新调查历史，可以重置无进展计数，但不提供支付真值；独立最大 cycles 仍阻止无限查询。Recovery Recheck 的重试上限由 Step 9 Policy 控制，不被通用 no-progress 上限提前截断。

终态由每次 poll、claim、resume、read reservation 检查。VerifiedClosureService 在关闭同一事务内取消未完成 Work。迟到 timer、signal、receipt 或重复 claim 不得重开 CLOSED/CLOSED_VERIFIED。

## 实际 Demo

仓库根目录执行：

```powershell
.\.venv\Scripts\python -m scripts.demo_orchestration --scenario wait-resume
.\.venv\Scripts\python -m scripts.demo_orchestration --scenario effect-verification
.\.venv\Scripts\python -m scripts.demo_orchestration --scenario escalation-resolution
```

全部使用 FakePlanner、临时 SQLite 和受控模拟时钟。S6 Demo 的人工审批仅由外部 synthetic fixture 模拟，Orchestrator 不拥有签发／批准权限。`--output path.json` 可保存完整结构；包含 Case、Work、lineage、Audit、真实查询结果与结案记录。

| Demo | 本次实际结果 |
| --- | --- |
| S8 wait-resume | 4 个 Run，3 次 resume，4 次 PAYMENT TIMEOUT，ESCALATED；时间推进三次均没有生成 Evidence |
| S6 effect-verification | APPLIED → real read → INCONCLUSIVE → 外部模拟状态收敛 → requirement batch → PASS → CLOSED_VERIFIED；2 个 verification cycles，16/24 Tool calls |
| escalation-resolution | 时间 +60s 仍不能 claim；SOURCE_RECOVERED signal 不生成 Evidence；恢复后真实 PAYMENT TIMEOUT，Case WAITING，1/24 Tool calls |

实际输出： [WAIT](examples/orchestration-wait-resume.json)、[Effect Verification](examples/orchestration-effect-verification.json)、[Escalation Resolution](examples/orchestration-escalation-resolution.json)。S8 没有 PAYMENT_FINALITY Evidence 时，输出空数组代表“未获得支付终态”，不得解释为没发生支付。

## Benchmark Stage Handoff Gap 与 Guidance

定向回归 `test_benchmark_policy_blocked_verification_uses_durable_resume` 保留旧限制：WAITING Case 直接调用 PAYMENT 必须被 CASE_POLICY_BLOCKED。新的合法路径是 persisted report → verification work → 到期 claim → Resume CAS → fresh Tool read → Evaluator。没有放宽 CasePolicy、Evaluator 或增加 Tool Budget。

原 Step 12.1 的 288-run v2 benchmark 是历史产物。此阶段不重跑或改写 raw-runs、summary.json、cases.json 来制造改善；`benchmark/summary.md` 与生成器只把误导性的 `Errors / blocked` 表头改为 `Errors`。Step 13 Demo／回归不被冒充为新的公平 benchmark 对照结果。

`InvestigationGuidanceService.build_result()` 返回 frozen `GuidanceBuildResult(bundle, status, degradation)`。Planner 与 benchmark checklist/telemetry 消费同一次返回值；不再读共享可变 `last_status / last_degradation`。旧 build()/last_* 仅兼容历史调用，并以 ContextVar 隔离；并发测试证明一个 AVAILABLE 调用不会被另一个 RETRIEVAL_FAILED 调用污染。

## 本阶段保留的边界

尚未实现 System Registry。真实系统注册、Tool capability discovery、deployment 可查询接口、Signal source/actor authentication、企业调度服务与跨服务运维配置留到该阶段。没有 UI-1、真实资金移动、自动 L2 Approval、通用 Workflow Engine、Agent 自评结案或面试包装工作。

## 最终验收（2026-09-12）

以下为 Step 13 历史验收；Step 13.1 增量验收另列于文末。

最终功能代码共收集 1137 个测试实例：原 1075 个，加 60 个 Orchestration 和 2 个 Guidance 调用隔离测试。

| 验证 | 实际结果 | 耗时 |
| --- | --- | --- |
| SQLite 编排专项 | 60 passed | 42.87s |
| SQLite 全量 | 1134 passed, 3 skipped | 802.62s |
| PostgreSQL 全量 | 1135 passed, 2 skipped | 976.68s |

两库全量均包括双 Worker claim、同 claim Resume CAS、同 claim process 只启动一个 Run、过期 lease reclaim、旧 Worker complete/read fence、原子 WAIT 回滚、Effect/outbox 交接崩溃、终态／tenant fence、S8 UNKNOWN、S6 PASS closure 和原 CASE_POLICY_BLOCKED 定向回归。没有放宽旧测试断言来提高结果。

两个 Live LLM 测试未开启，在两库均跳过；SQLite 另跳过 PostgreSQL 专用测试。真实 Provider SDK + MockTransport 离线测试在全量中执行，无外网模型调用。仅有既有 Starlette/AnyIO BlockingPortal 弃用提示。

复现命令（PostgreSQL URL 使用专用本地测试数据库，fixture 为每个测试创建隔离 schema）：

```powershell
.\.venv\Scripts\python -m pytest tests/test_orchestration.py -q --basetemp .local/pytest-orchestration
.\.venv\Scripts\python -m pytest -q --basetemp .local/pytest-orchestration-sqlite
# 先配置 TEST_POSTGRES_URL
.\.venv\Scripts\python -m pytest -q --postgres --basetemp .local/pytest-orchestration-postgres
```

## Step 13.1：Recovery-caused revision rebasing

原 self-stale 原因是：Work expected R10 → Read Recovery 发布 Evidence 推进 Case R11 → Resume 仍比较 R10。修复不能忽略所有 revision mismatch，否则另一个 Worker 的 dispatch、Evidence publication 或生命周期操作也会被错误吸收。

`recover_before_resume(claim)` 先验证当前 lease、Case status 和 expected revision；已 stale 的 Work 直接取消，不先恢复。`ReadObservationRecoveryService.recover_if_owned(case_id, call_id, repository, claim)` 随后在同一个 **Case lock / SQL transaction** 内：

1. 再核验 Work ownership、Case status/revision、非终态及尚未启动 Run。
2. 调用原确定性 Recovery publication，沿用 CaseCall/Observation 的 simulation、grant、request、content hash、dispatch correlation 校验。
3. 核对 durable ReadDispatchRecoveryRow、Call 的 OBSERVED 状态、Observation 绑定、EvidenceOrigin 及 recovered refs。
4. 仅 OBSERVATION_RECOVERED 可更新本 Work 的 expected_case_revision，并记录 RECOVERY_REBASED audit；其他结果若出现 revision 变化则拒绝。

没有接受 `old_revision / new_revision / recovery_caused=True` 的外部 rebase API。原 Work ID、claim token 和 Tool Budget 保持不变。Evidence、Origin、Recovery receipt、Case revision 与 Work rebase 一起提交或一起回滚。提交后 Worker 崩溃，后继 lease 仍可基于已经 rebased 的同一个 Work 继续。

不同事务中的合法 mutation 无法插入这个 Case lock；若发生在任意两次读恢复之间、Effect lookup 期间或最终 Resume CAS 前，当前 revision 将与已 rebased 的 expected revision 不同，仍取消 Work。回归包含真实并发 mutation，不能把外部 R12 接受为本次 Recovery 的 R11。

新增 frozen `ResumeRecoveryResult` 保存 case/work、before/after Case revision、read recovery receipts、recovered Evidence refs、effect recovery receipts、semantic_progress 和 prepared_blocked；它是编排回执，不是 Evidence。Work 同时持久化恢复前 progress 基线，防止 Recovery 已提交但进程中断后丢失这段进展。同一 claim 的 `recovery_claim_token` 防止重复投递并发调用 Effect Recovery；中途崩溃由 lease successor 接续。

## Step 13.1：Orchestration semantic progress

`OrchestrationProgressFingerprint` v2 使用：

- Evidence content/provenance fingerprint；新的 Tool Observation 保持原有时间序列语义。
- 每个 Effect 的 `(effect_id, action, status, target_hash, payload_hash, unresolved/resolved)`，稳定排序。
- 当前 unresolved verification requirements 的去重排序集合。

不读取 WorldState/GroundTruth；不纳入 Case updated_at、Tool Budget、Ledger.updated_at、attempt count、worker、lease 或调度时间。独立 Evaluator 的只读计算用于取得前后 requirement 集合，未额外改变 Evaluator contract、持久化规则或关闭权限。

捕获顺序是 **progress BEFORE → Recovery → Resume/Read → Evaluation → progress AFTER**。第一次基线也取得真实 requirement 集合，不把“首次初始化空集合”误算为新知识。旧 evidence-only fingerprint 没有 v2 标记时不会直接与新 hash 比较，避免算法升级本身重置 no-progress。

UNKNOWN → APPLIED 或 FAILED_CONFIRMED 是确定性编排状态变化，即使没有新增 Evidence 也重置 no-progress。UNKNOWN → UNKNOWN、单纯新 RecoveryAttempt／时间戳不构成变化。EFFECT_FINALITY → POST_EFFECT_MESSAGE_STATUS 是 requirement 变化；同一集合仅顺序改变不算进展。

**Orchestration progress 不等于 Business Truth progress。** APPLIED 只允许进入效果后的业务验证；并不会证明三方已经收敛或让 Evaluator PASS。S8 恢复 Timeout Observation 后仍只有 lookup unavailable 的证据，不能生成支付 FAILED / NOT_EXECUTED。

## Step 13.1：Recovery backoff handoff

RECOVERY_RECHECK 由专用有限分支处理，不先启动 Agent Run，也不因为 EFFECT_FINALITY 没有普通 Read Tool 就立即人工升级。每次 tick 最多调用一次绑定 effect 的 `effect_recovery.recover()`；normal resume 也最多尝试一个未决 effect。PREPARED 的原授权检查及 DISPATCHED/UNKNOWN 禁止 redispatch 的规则保持不变。

| Step 9 当前结果 | Work 处理 |
| --- | --- |
| APPLIED / FAILED_CONFIRMED 已确定 | 当前 Recovery Work COMPLETED；复用原 effect_handoff 创建验证／人工 Work，不 rearm |
| STILL_UNKNOWN / BUSY_OR_NOT_DUE，尚未达到 limit | 同一 Work 重新 PENDING，释放 claim，按 Step 9 next_eligible_at 重新到期 |
| requires_escalation 或 Step 9 max_attempts 已到达 | Work BLOCKED，创建 EFFECT_UNRESOLVED operator followup；Ledger 保持 UNKNOWN |

正常重试的 not_before 等于 Step 9 next_eligible_at；若还有 grace 或其他 Step 9 Worker 的有效 lease，则取它们中更晚的时间。不会提前 lookup，也不会 sleep 或 while UNKNOWN。默认 attempts/backoff 沿用注入的 Step 9 RecoveryPolicy；测试使用不同上限验证并非写死三次。

所有 Recovery Work producer 在 Case lock 下按 `(case, effect source_ref, RECOVERY_RECHECK)` 复用原记录，包括旧 key 和不同触发原因。同一 effect 不产生第二个 active Recovery Work，更不会产生第二个业务 Effect。无新增数据库表、Capability 或业务执行入口。

## Step 13.1 验收范围

新增回归覆盖：orphan WAIT Resume 不 self-stale、原子回滚与提交后 crash、同 Work/claim、无第二次预算、fresh Snapshot 可见、foreign Observation、stale lease、终态拒绝、并发 mutation 拒绝及并发 recovery publication 防重；语义状态/requirement 进展与非语义变化；Step 9 rearm/backoff/max attempts、busy lease、单 active work、同 claim 单 recovery invocation、S6 先 INDETERMINATE 后 FOUND_APPLIED，再通过真实 MESSAGES Observation 验证 CONSUMED。

旧测试仅调整了两个连接点：Recovery-before-Planner 的 spy 改为 lease-aware 入口；PREPARED 正例 Work 明确绑定真实 effect_id。原安全断言不变。Benchmark v2 历史结果、Evidence extraction、Hypothesis、Remediation/Approval/Capability、资金真值和 Evaluator closure semantics 均未修改。

### Step 13.1 最终测试结果（2026-09-12）

最终代码共收集 1175 个测试实例，较 Step 13 新增 38 个；以下均为实际运行结果。

| 验证 | 结果 | 耗时 |
| --- | --- | --- |
| 新增 Recovery-aware Resume 专项 | 38 passed | 58.14s |
| SQLite 编排并发／原子性专项 | 12 passed, 86 deselected | 13.72s |
| PostgreSQL 编排并发／原子性专项 | 12 passed, 86 deselected | 20.93s |
| SQLite 全量 | 1172 passed, 3 skipped | 1088.90s |
| PostgreSQL 全量 | 1173 passed, 2 skipped | 1303.58s |

两库全量包含全部 Orchestration、Recovery、Agent Runtime 与 Evaluator 测试。两项在线 LLM 测试未启用，在两库均跳过；SQLite 另跳过 PostgreSQL 专用测试。Provider SDK + MockTransport 离线测试实际执行。仅有既有 Starlette/AnyIO BlockingPortal 弃用提示。

```powershell
.\.venv\Scripts\python -m pytest tests/test_resume_recovery_integrity.py -q --basetemp .local/pytest-step131-new-final
.\.venv\Scripts\python -m pytest tests/test_orchestration.py tests/test_resume_recovery_integrity.py -q -k 'concurrent or two_workers or atomic or stale_lease or lease_expiry' --basetemp .local/pytest-step131-final-concurrency-sqlite
# 配置 TEST_POSTGRES_URL，指向专用本地测试数据库；fixture 使用隔离 schema。
.\.venv\Scripts\python -m pytest tests/test_orchestration.py tests/test_resume_recovery_integrity.py -q --postgres -k 'concurrent or two_workers or atomic or stale_lease or lease_expiry' --basetemp .local/pytest-step131-final-concurrency-postgres
.\.venv\Scripts\python -m pytest -q --basetemp .local/pytest-step131-full-sqlite
.\.venv\Scripts\python -m pytest -q --postgres --basetemp .local/pytest-step131-full-postgres
```

本次没有重跑或改写历史 Benchmark v2 产物，也没有启动 System Registry、UI-1、Money Movement 或 Interview Packaging。

## Step 13.2：Requirement 不等于 Work Type

`UnresolvedVerificationRequirement` 只声明缺什么，不指定 Worker、Tool 或重试机制。`orchestration/routing.py` 的 `RequirementRouter` 返回冻结的 typed `RoutedRequirement`，使用三种 `RequirementRoute`：

| Requirement / 当前可信状态 | Resolution Mechanism |
| --- | --- |
| 静态 `VERIFICATION_TOOLS` 中的业务 Evidence requirement，且对应 Tool 在当前 Case scope 内 | READ_VERIFICATION → VERIFICATION_REQUIRED |
| EFFECT_FINALITY 的 effect_ref 指向当前 Case 的 PREPARED / DISPATCHED / ACCEPTED / UNKNOWN Ledger，且有可用 Recovery state、尚未 requires_escalation | EFFECT_RECOVERY → RECOVERY_RECHECK |
| RECOVERY_FINALITY | 枚举当前 Case 未决 Ledger，逐个按 Recovery state 路由；不创建 aggregate Read Work |
| Step 9 requires_escalation | OPERATOR_FOLLOWUP(EFFECT_UNRESOLVED)；Ledger 仍保持 UNKNOWN |
| PROVENANCE、SAFE_POLICY 等没有明确 machine resolver 的 requirement；缺失／外来 effect 引用；缺少 Recovery state；Read Tool 不在 scope 内 | OPERATOR_FOLLOWUP，要求人工信号 |

只有真实 Read 路由的 requirements 才进入 read batch。EFFECT_FINALITY / RECOVERY_FINALITY 永远不进入该 batch，不伪造 Tool。存在其他合法业务 Read 不妨碍 Recovery backoff；但不能由 Finality 的 NO_TOOL 错误提前升级 Case。没有把所有 INCONCLUSIVE 都解释成“再查一个 Tool”。Evaluator 的 requirements、verdict、资金真值与 Closure authority 均保持原样。

路由只使用 requirement 类型、effect_ref、当前 Case Tool scope 和同一 Case lock 下读取的 Ledger / Recovery state；reason_code、自然语言解释不决定路线。Recovery work 的 source_ref 始终是 effect_id，继续使用 `create_work` 的 effect source 去重。Evaluator 与 effect_handoff 并发创建也在事务内复用同一个 Work，不做事后清理。

新建 Recovery Work 使用 Step 9 next_eligible_at / 有效 lease 作为到期下界；已有 Work 原样复用，不能覆盖其 backoff。实际 lookup 的 grace、backoff、max_attempts 和 requires_escalation 全由原 Step 9 Repository 校验，Handoff 不复制 retry counter 或另设 max_attempts。Step 9 在完成尝试或检查到耗尽时发布 requires_escalation；Handoff 消费这个可信结果。EFFECT_UNRESOLVED 人工跟进沿用该 effect 原 Recovery Work 的 source identity，兼容 Step 13.1 已有跟进；没有 Recovery Work 时才使用 effect_id，避免两个 producer 创建重复跟进。

## Step 13.2：Recovery 与 Investigation no-progress 隔离

保留原 `CaseOrchestrationState.no_progress_count` 字段及已有持久值，现在明确只计 Investigation / Verification cycle。没有表迁移、字段重命名或启动时清零；无法可靠拆分的旧计数不自动扣减。

- RECOVERY_RECHECK UNKNOWN → UNKNOWN：semantic_progress=false，普通 no_progress_count 保持原值。
- UNKNOWN → APPLIED / FAILED_CONFIRMED：算确定性编排进度，可将普通 no-progress 重置为 0。
- Recovery 尝试仍由 Step 9 attempt_count / max_attempts 限制，不增设第二套预算。
- 普通 Investigation / Verification 没有进展仍递增，达到原上限仍升级；原 resume / verification / Tool Budget gate 不变。

本次回归覆盖报告与 Recovery Work 同时存在、backoff 前 claim_next、两次 UNKNOWN 后 APPLIED 与真实 MESSAGES Read、Step 9 limit 后单一人工跟进、Payment verification 不受 Recovery 空转计数影响，以及两库并发 handoff。测试保留真实 Evaluator 时间语义：超过收敛宽限期时允许报告变成 FAIL，不为测试强行改成 INCONCLUSIVE。

两项耗尽报告的幂等测试使用现有可配置 `EvaluationPolicy` 的较长 synthetic 收敛窗口，以单独验证 INCONCLUSIVE + requires_escalation 路由；没有伪造 Report 或修改生产 Evaluator。默认窗口下的完整重试测试仍保留真实 FAIL 结果。

### Step 13.2 最终验收（2026-09-12）

最终代码收集 1203 个测试实例，较 Step 13.1 新增 28 个。旧 UNKNOWN → UNKNOWN 回归只调整普通 no-progress 的预期值：原来从 2 增加到 3，现在保留 2；普通 Investigation 的递增和旧状态兼容断言均保留。

| 验证 | 实际结果 | 耗时 |
| --- | --- | --- |
| Step 13.2 最终专项 | 28 passed | 38.24s |
| SQLite 编排并发／原子性／路由边界 | 14 passed, 112 deselected | 10.59s |
| PostgreSQL 编排并发／原子性／路由边界 | 14 passed, 112 deselected | 15.46s |
| SQLite 全量 | 1200 passed, 3 skipped | 1005.27s |
| PostgreSQL 全量 | 1201 passed, 2 skipped | 1238.04s |

两库全量包含 Orchestration、Recovery、Evaluator、Agent Runtime 及全部既有安全回归。两个在线 LLM 测试未启用；SQLite 另跳过 PostgreSQL 专用测试。Provider SDK + MockTransport 离线测试实际执行；仅有既有 Starlette/AnyIO BlockingPortal 弃用提示。

```powershell
.\.venv\Scripts\python -m pytest tests/test_finality_routing.py -q --basetemp .local/pytest-step132-frozen-target
.\.venv\Scripts\python -m pytest tests/test_orchestration.py tests/test_resume_recovery_integrity.py tests/test_finality_routing.py -q -k 'concurrent or two_workers or atomic or stale_lease or lease_expiry or aggregate_finality' --basetemp .local/pytest-step132-concurrency-sqlite
# 配置 TEST_POSTGRES_URL 为专用测试数据库；各测试使用独立 schema。
.\.venv\Scripts\python -m pytest tests/test_orchestration.py tests/test_resume_recovery_integrity.py tests/test_finality_routing.py -q --postgres -k 'concurrent or two_workers or atomic or stale_lease or lease_expiry or aggregate_finality' --basetemp .local/pytest-step132-concurrency-postgres
.\.venv\Scripts\python -m pytest -q --basetemp .local/pytest-step132-full-sqlite
.\.venv\Scripts\python -m pytest -q --postgres --basetemp .local/pytest-step132-full-postgres
```

未修改 Evidence、Payment Identity、Hypothesis、Remediation、Approval/Capability、Effect idempotency、Recovery proof、Evaluator truth、Closure、Tool Budget 或历史 Benchmark v2 产物。未开始 System Registry、Vector/Embedding、UI-1、Money Movement 或 Interview Packaging。
