# Step 13 — Durable Case Orchestration

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

`CaseResumeService.resume(WorkClaim)` 不提供 `resume(case_id)`。它先运行已有 read dispatch recovery、再检查／恢复未决 SideEffect，然后在锁内检查：

1. 仍拥有未过期 lease；Case 尚未 CLOSED/CLOSED_VERIFIED。
2. 当前 Case status/revision 与 Work 创建时的值完全一致。
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
| INCONCLUSIVE | 从 unresolved verification requirements 创建有限验证 Work |

outbox 有租户／状态／sequence 索引，有限扫描。工作创建完成但 ack 前崩溃可幂等重放；陈旧报告被取消，不阻塞后续报告。没有将 Report 或 Signal 写成 Evidence。

验证 Worker 的静态映射仅覆盖已有 PAYMENT、GUARANTEE、FUND、ASSET、ACCOUNTING、MESSAGES、ASSET_DELIVERY 能提供的指定 requirement。每一轮只读取同一报告中的可用、去重 Tool 集合，不扫 ALL_TOOLS；未知 requirement 留给人工。每次读取都重新组装 Snapshot，经 CaseToolExecutor、租约 fence、原 order/tool scope 和原 Tool Budget。批次中耗尽 Tool 预算会停止并升级。有限批次读完后再评估，避免对一半刷新、一半陈旧的状态过早判断。

## 独立配额、进度与 Terminal Fence

默认 `max_resume_cycles=5 / max_verification_cycles=5 / max_no_progress_cycles=3`；每项有模型上限。Demo 在 Case 开始前配置 resume=3、verification=5、Tool Budget=24。开始恢复后不能重设 cycles；也不会增加原 Tool Budget。

跨 Run fingerprint 来源于真实 Evidence（含新的 lookup/time/provenance），另记录本轮 verification requirements 和前后指纹。仅 Case revision、状态切换或 budget 使用量变化不能算进展。新 Timeout 是新调查历史，可以重置无进展计数，但不提供支付真值；独立最大 cycles 仍阻止无限查询。无新知识或达到配额时生成持久化 operator followup。

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
