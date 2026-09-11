# Step 10：Independent Evaluator 与 Verified Closure

`REPLAY APPLIED; MESSAGE CONSUMED; BUT BUSINESS CASE NOT VERIFIED`。

这不是异常的演示结果：重放只解决一个消息效果。担保方、资产方和账务尚未收敛时，Case 不能关闭。Planner、Remediation Planner、Executor 和 Recovery 都没有结案权限；模型说明、Tool SUCCESS、Hypothesis CONFIRMED、Ledger APPLIED 均不能替代业务验收。

所有身份、账户、订单与效果仍为 synthetic fixtures。Evaluator 不依赖 LLM，不调用 Tool、status resolver 或 Recovery，不访问模拟 Oracle。

## 实际文件与服务调用

```text
src/credit_harness/evaluation/
  models.py       verdict、维度、缺口、Snapshot、Report、Closure 类型
  contract.py     版本化业务契约与 EvaluationPolicy
  snapshot.py     tenant-scoped durable input read 与当前事实投影
  evaluator.py    只读、确定性业务判定
  tables.py       append-only Report 与唯一 CaseClosure 表
  repository.py   显式审计持久化
  closure.py      唯一 CLOSED_VERIFIED transition 与事务 CAS
src/credit_harness/evidence/verification.py
tests/support/evaluation_fixture.py
tests/test_evaluator.py
scripts/demo_evaluator.py
```

可信服务的用法：

```python
evaluator = IndependentEvaluator(cases, clock=clock)
reports = EvaluationRepository(cases)
report = evaluator.evaluate(case_id)       # 只读，不写 Case/Report/Ledger
reports.record(report)                    # 显式 append-only 审计
if report.overall_verdict == EvaluationVerdict.PASS:
    result = VerifiedClosureService(evaluator).close(report)
```

这些服务没有注册为 Agent Tool，也没有新增给模型的关闭动作。`record()` 只是存储，不是证明签发权：即使调用者构造 PASS、重算哈希并保存，Closure 仍重新计算完整 Report，拒绝不一致结果。普通 CaseRepository.pause、Planner candidate schema 与 Agent Runtime 都无法触发该 transition。

## 维度、判定与缺证据

| 维度 | 主要条件 |
|---|---|
| MONEY | 当前权威支付终态及请求绑定；不从 Fund/HTTP/Callback 状态推导钱的终态 |
| IDENTITY | 复用完整 PaymentIdentityWitness；金额、币种、原请求、交易、客户、受益人、账户一致 |
| STATE_CONVERGENCE | Fund、Guarantee、Asset、Accounting 与 outcome path 一致；适用的 Callback/Delivery 完成 |
| SIDE_EFFECT | 没有未决操作；APPLIED 业务效果有目标一致的后续独立 Read Evidence |
| EVIDENCE_SUFFICIENCY | provenance、当前性、完整性、相关 safety gaps 与 pending reads |
| POLICY | 所有 Ledger action 属于允许 Catalog，且授权/效果身份完整；L3/L4 或 money/order-state/bulk 禁止 |
| RECOVERY | 没有未决恢复、活跃 attempt/lease、升级标记对应的不确定性 |
| OUTCOME | 资金、身份、状态和效果要求合并后的业务结果 |

每个维度为 PASS / FAIL / INCONCLUSIVE / NOT_APPLICABLE。V1 要求覆盖全部八个维度；任一 required FAIL 优先，其次是 INCONCLUSIVE，剩余 applicable required 维度全部 PASS 才能总体 PASS。NOT_APPLICABLE 是契约明确不适用，不是缺证据的别名。

身份 MISMATCH 为 FAIL；UNKNOWN 为 INCONCLUSIVE。正确身份不会抵消不安全过程。Accounting 没有当前结果、查询 timeout、Payment 未知、APPLIED 后缺 Read 均不能被当成“确认失败”；报告用严格 Enum reason codes 和 `UnresolvedVerificationRequirement` 列出所缺条件，不生成 Tool Call、概率或模型 rationale。

## Verification Contract

`CreditGuaranteeDisbursementVerificationContractV1`，独立版本 `VERIFICATION_CONTRACT_VERSION=1`，根据 Evidence 选择两条路径：

| 条件 | SETTLED_PATH | NO_DISBURSEMENT_PATH |
|---|---|---|
| Payment | 当前 primary/complete 明确 SETTLED | 当前 primary/complete 明确 NOT_EXECUTED，fund-request subject 与原请求相同 |
| Identity | 完整 Payment Identity MATCH | 无支付交易；支付账户身份维度 NOT_APPLICABLE，仍必须绑定原请求 |
| Fund / Guarantee / Asset | SUCCESS | FAILED |
| Accounting projection | 当前 entry present=true | 当前 entry present=false |
| Callback | 当前 gateway received、验签成功、同 event 的 message CONSUMED | 本路径不要求不存在的成功回调 |
| Asset delivery | DELIVERED | 本路径不要求成功放款通知 |

NO_DISBURSEMENT 绝不由 NOT_FOUND、Timeout、Fund FAILED 或 Asset FAILED 单独选择。已有 PaymentRecord 的 NOT_EXECUTED 是 Simulator Payment System 的显式无效果业务状态；它与查询缺失是不同字段、不同语义。V1 不支持资金冲正：同一原请求历史已有可信 SETTLED，后来出现 NOT_EXECUTED 时报告 MONEY_STATE_CONTRADICTION，不能抹掉历史支付。

OPEN SAFETY_CRITICAL Gap 默认阻止 PASS。唯一 V1 路径例外是 NO_DISBURSEMENT 下的 PAYMENT_IDENTITY gap：既有该 gap 要求一个 SETTLED transaction，而该路径已经以请求级权威事实证明无交易，故它不适用；请求关联和支付终态 gap 仍须满足。没有根据模型摘要或 Scenario 选择例外。

契约诚实受当前 Claim 覆盖约束：账务目前只有 ACCOUNTING_ENTRY_PRESENT，V1 只验证本地业务投影存在/不存在，不声称验证双重记账、余额、账务金额或完整会计正确性。要支持这些要求，需要先扩展真实 Observation/Evidence contract。

## 当前性、完整性与时间

所有业务时间和报告/台账时间均 timezone-aware，Evaluator 注入 clock。生产应使用统一、可信的时间域；synthetic fixture 明确对齐业务时钟与授权/效果时钟，不使用 sleep。

先按 tool + 精确 query scope 取最后一次观测，再检查 primary、COMPLETE、CURRENT、source_as_of 与新鲜度。后来 timeout、stale cache 或 incomplete 不能使更早的 SUCCESS 重新成为当前事实。相同观测时刻的互斥值保留，不以任意 ID 打破平局。

`EvaluationPolicy` 默认 `evidence_max_age_seconds=300`、`convergence_grace_seconds=30`，版本为 `EVALUATION_POLICY_VERSION=1`。质量约束限定当前 outcome path 的相关来源；合法未放款不被无关 Callback NOT_FOUND 阻断。

刚执行效果时，PROCESSING/PENDING、未投递、账务未生成等异步中间态在 grace 内是 INCONCLUSIVE。超出窗口仍明确不收敛则 FAIL。互斥终态、身份不符或明确失败不靠窗口冲抵。窗口优先以 APPLIED 时间为锚点，没有效果时使用已观察业务事件时间。时间跨越 freshness/grace 边界后，派生状态指纹会变化，使旧 PASS 不能关闭。

## Evidence provenance 与身份边界

Evidence 边界的 `read_verified_evidence()` 只读核验：Observation hash/ID/tool/query/order、simulation/grant、dispatch correlation、CaseCall binding、EvidenceOrigin coverage，以及 stored Evidence 是否等于原确定性提取结果。篡改值后重新写一个 Evidence JSON 不会成为可接受事实。

重复 Evidence 的 durable ID 保持不变；若它有多个原始来源回执，核验每个 origin 后可用最新回执的观测时间评价当前性。Snapshot 指纹包含完整 origin/call/receipt 内容身份，报告只引用 Evidence IDs。原始回调、身份文档、credential、Capability signature、异常原文和模型私有思考均不进入报告。

支付身份复用现有 deterministic service，只处理 opaque refs。Evaluator 不读 PII Vault，不重新比较身份证/银行卡。测试通过 AST 禁止 Oracle/LLM/adapter import，并在运行时禁止 WorldState 解析、检查 SQL 未访问 world_snapshots / evaluation_ground_truth / observation_faults / capability_signatures。

## APPLIED 的后续独立证据

Replay APPLIED 后必须有 MESSAGES Evidence：同 message_ref、同 callback event，CONSUMED。Redelivery APPLIED 后必须有 ASSET_DELIVERY Evidence：同 observation 中的 delivery event ref 与 target 相同，状态 DELIVERED。

要求 `observed_at > ledger.updated_at`、`source_as_of >= ledger.updated_at`，事件时间不得早于原效果创建时间。因此执行前的 CONSUMED、执行后读取一个旧消费事件、错 message、错 callback 或错 delivery 都不能作为效果证明。真实消费事件通常早于 APPLIED acknowledgement，故事件锚点使用原操作创建时间，独立 Read 则必须晚于结果确认时间。

Recovery APPLIED 同样要求在恢复确认后重新 Read。Recovery Proof 不能直接写业务 Evidence。效果 FAILED_CONFIRMED 也不自动使整个 Case FAIL：若当前业务已通过其他正常路径收敛，仍按事实验收。L1 review/reconciliation 任务完成只代表行政动作，不能替代业务维度。

## Recovery 与 pending reads

PREPARED、DISPATCHED、ACCEPTED、UNKNOWN 任一 Ledger 均阻止 PASS。恢复查询三次仍未知、requires_escalation、未完成 attempt、活跃 lease 都保留 INCONCLUSIVE；Evaluator 不调用 resolver、不试图恢复、不重发。

任何 DISPATCHED 或 ERROR Call 均保守视作 pending publication。即使 recovery 曾 NOT_FOUND，仍不能证明原响应以后不会到达；必须经过 Step 9 原 provenance 发布使 Call OBSERVED，才能解除阻断。Evaluator 不将“查了很多次”当成充分性。

## VerificationSnapshot 与 Report

VerificationSnapshot 独立于有限的 Planner ReasoningContextSnapshot，包含以下内容身份：

- Case / tenant / revision 与完整 Case fingerprint。
- durable Evidence、origins、Observations provenance fingerprint。
- Payment Identity、当前状态/Gap 与时间资格 fingerprint。
- Ledger、Call history、Recovery state/attempt fingerprint。
- 当前授权策略/Catalog metadata 与效果身份绑定 fingerprint。
- Evaluation Policy / Verification Contract 的版本及完整配置 fingerprint。

`verification_snapshot_id=SHA256(canonical payload)`，使用最终 Pydantic JSON 表示（包括 UTC 的 `Z`）计算，导出方可直接重算。没有 raw Evidence 全量复制或自然语言 Prompt。EvaluationReport 保存 Snapshot、八个维度、typed reasons、Evidence/Effect refs 和未决条件。

`evaluation_run_id=UUID` 用于一次调用审计；`report_id` 是不包含 run ID / created_at 的内容哈希。同一 durable 状态且时间资格未变化时，重复 evaluate 得到同一个 business report ID。不同 run 可 append-only 保存；同一个 run 不能覆盖旧结果。

## Evaluate 与 Close 的事务分工

evaluate 仅 SELECT：SQLite 显式 BEGIN 建立读取快照，PostgreSQL 对 Case 取共享读锁。它本身不写 Report、Evidence、Case 或 Ledger。Report 审计由 repository.record 显式写入。

close 在事务里先锁 tenant-scoped Case，读取 CaseCall、Evidence、Effect、Recovery，再核对持久 Report 和唯一 Closure Record。所有正常 producer 也先锁 Case，包括此次补齐的普通 SideEffect receipt transition，因此不会与 Closure 构成反向锁序。

关闭要求：

1. 输入是严格 typed、hash 正确且已持久化的 PASS Report。
2. 当前 Case revision、所有 durable fingerprints、策略和契约完全匹配。
3. 在锁内重新执行完整 deterministic evaluation，仍为 PASS，business report ID 相同。
4. Case 条件 UPDATE 成功，原子写入唯一 CaseClosureRow。

Evidence publication、Call reservation、Ledger mutation、Recovery 状态变化或策略升级夹在 evaluate/close 中间，均导致 STALE_EVALUATION，不能使用旧 PASS。SQLite 写锁、PostgreSQL 行锁及唯一 Case PK 支持并发：两个关闭者只有一个 transition，另一方返回 ALREADY_CLOSED_VERIFIED；关闭与新 dispatch 竞争也不能同时成功。

普通 CLOSED 作为 legacy 值保留；新验收只能由 VerifiedClosureService 写 CLOSED_VERIFIED。关闭后禁止新 Tool dispatch、pause 重开、新 Capability、prepare/dispatch；历史审计可读，本阶段没有 reopen。再次关闭同一报告不会修改 revision 或新增 Closure，但若发现新增 pending work / 证据变化 / 未决效果，会报告 CLOSURE_INVARIANT_VIOLATION，不掩盖异常。

CaseClosureRecord 绑定 report/run/snapshot/Evidence/Ledger fingerprints、policy/contract version 与 closed_at。它是历史事务时刻的通过证明；报告随时间变旧不会自动重开 Case。Ledger 仍保留原 APPLIED 等操作状态，不把行政动作统一改名 VERIFIED。

## 实际 Demo

```powershell
$demoBytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($demoBytes)
$env:CAPABILITY_SIGNING_SECRET = [Convert]::ToBase64String($demoBytes)
.\.venv\Scripts\python scripts/demo_evaluator.py --scenario s6-partially-repaired
.\.venv\Scripts\python scripts/demo_evaluator.py --scenario s6-converged
.\.venv\Scripts\python scripts/demo_evaluator.py --scenario unknown-effect
.\.venv\Scripts\python scripts/demo_evaluator.py --scenario identity-mismatch
.\.venv\Scripts\python scripts/demo_evaluator.py --scenario no-disbursement
```

`--output .local/evaluation.json` 可保存完整输出。外部收敛只在 tests/support/evaluation_fixture.py 改变 synthetic world，然后通过真实 HTTP Read Tool → Observation → Evidence 形成事实；没有直接写 Evidence、Graph 或 Report 结论。

| 实际导出 | Overall | Case | Evidence 数量 |
|---|---|---|---|
| [S6 局部修复](examples/evaluation-s6-partially-repaired.json) | INCONCLUSIVE | INVESTIGATING | 42 |
| [S6 完整收敛](examples/evaluation-s6-converged.json) | PASS | CLOSED_VERIFIED | 72 |
| [未决效果](examples/evaluation-unknown-effect.json) | INCONCLUSIVE | INVESTIGATING | 72 |
| [身份不符](examples/evaluation-identity-mismatch.json) | FAIL | INVESTIGATING | 34 |
| [明确未放款](examples/evaluation-no-disbursement.json) | PASS | CLOSED_VERIFIED | 19 |

局部修复的 MONEY / IDENTITY / SIDE_EFFECT 为 PASS，STATE_CONVERGENCE / OUTCOME 为 INCONCLUSIVE；完整收敛八个维度全部 PASS。数量仅作演示记录，绝不是关闭条件。

## 测试与后续边界

Step 10 新增 72 个测试实例，包括从 UTC JSON 重算 Snapshot ID 的回归；总计 914 个。以下为最终代码的实际测试结果：

| 测试 | 结果 | 耗时 |
|---|---|---|
| SQLite full suite | 911 passed, 3 skipped | 516.55s |
| PostgreSQL full suite | 912 passed, 2 skipped | 636.57s |
| SQLite closure concurrency | 2 passed | 2.45s |
| PostgreSQL closure concurrency | 2 passed | 4.10s |

两种数据库都跳过两个需要显式启用和 API Key 的在线 LLM 测试；SQLite 另外跳过一个 PostgreSQL 专用测试。本次未调用在线 LLM。全量各有一个既有 Starlette/AnyIO BlockingPortal 弃用提示。并发测试分别验证双 Worker 关闭只发生一次，以及关闭与新 dispatch 竞争时只有一方成功。

全量命令为 `python -m pytest -q` 和配置专用测试库 `TEST_POSTGRES_URL` 后的 `python -m pytest --postgres -q`。单独并发验证使用 `tests/test_evaluator.py -k "two_workers_only_one_closure_wins or closure_and_new_dispatch_race_have_single_winner"`，PostgreSQL 加 `--postgres`。实际运行使用独立 `--basetemp` 并禁用 pytest cache。

主要覆盖：

| 验收边界 | 测试证据 |
|---|---|
| 两个合法 outcome path | SETTLED 完整收敛、S3 synthetic 无放款及 NOT_FOUND 不能选择无放款 |
| 金融身份 | 金额、币种、客户、受益人、账户、请求不符为 FAIL，缺失为 INCONCLUSIVE |
| 当前资金与历史 | stale/timeout 不复用旧 SUCCESS；历史 SETTLED 不被新 NOT_EXECUTED 抹掉 |
| 局部效果与业务 | APPLIED 无 Read、错 message、旧 Read 均不通过；CONSUMED 后业务仍未收敛不关闭 |
| 恢复不确定性 | UNKNOWN 三次仍 INCONCLUSIVE；Recovery APPLIED 后仍需新的 Read |
| 过程安全 | forbidden Catalog risk、未知 Action、篡改 Evidence、缺失 provenance 均 fail closed |
| 关闭 CAS | 新 Evidence、pending Call、Ledger/Recovery/策略/契约变化拒绝旧 PASS；伪造 PASS 不授予权限 |
| 并发与生命周期 | 两 Worker 单次关闭，关闭/dispatch 单胜，关闭后拒绝新工作；幂等关闭不掩盖异常 pending work |
| 隔离与审计 | AST 禁止 Oracle/LLM/adapter；运行时禁止 Oracle SQL/解析；Raw PII/rationale 不进 Report |
| 内容身份 | 相同状态 report_id 相同、run 不同；Report append-only；UTC JSON 哈希可重算 |

本阶段没有 Skill / Experience learning、System Registry、UI-1、Money Movement、Benchmark、LLM Judge、无限重查/修复循环或通用规则引擎。INCONCLUSIVE 返回结构化 verification requirements；FAIL 返回报告，由未来外层决定调查或升级，Evaluator 不变成 Planner。

Step 11 可以从 `CLOSED_VERIFIED + 绑定 PASS Report + ClosureRecord` 筛选 Verified Experience Candidate，并追溯原证据、策略和契约；本阶段不写 Memory、不把 APPLIED 案例直接包装为成功经验。
