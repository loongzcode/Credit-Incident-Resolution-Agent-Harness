# 金融三方业务 Simulator

本阶段实现业务世界、受限只读 Observation API 和测试，不包含 Agent、修复 Runtime、审批流程或在线 Case Evaluator。

## 已实现目录

```text
.
├── README.md                         # 总体项目契约与当前实现状态
├── pyproject.toml                    # 安装、CLI、pytest 配置
├── compose.yaml                      # 本地 PostgreSQL，绑定 loopback
├── .env.example
├── src/credit_harness/
│   ├── domain/
│   │   ├── enums.py                  # 全部业务/观测/故障状态 Enum
│   │   └── models.py                 # 七个系统及 WorldState
│   ├── simulator/
│   │   ├── scenarios.py              # S1–S8、事件快照、ground_truth
│   │   ├── faults.py                 # 受控的观测失真策略
│   │   ├── projections.py            # 每个工具允许看到的字段
│   │   └── service.py                # 权限→选视图→投影→记录 Observation
│   ├── tools/
│   │   ├── contracts.py              # 严格请求与判别联合响应 DTO
│   │   └── client.py                 # 仅 HTTP 的受限客户端
│   ├── persistence/
│   │   └── store.py                  # SQLAlchemy 表、种子、权限、时钟
│   ├── api/
│   │   └── app.py                    # 仅 POST /tools/{tool}
│   ├── settings.py
│   └── cli.py                       # 本地可信操作员命令
├── scripts/
│   └── inspect_scenario.py           # 逐个调用工具的人工检查脚本，无模型
├── tests/
│   ├── conftest.py                   # SQLite / PostgreSQL 隔离测试环境
│   ├── test_scenarios.py             # 每个 S 场景的明确行为断言
│   ├── test_observation_faults.py    # 超时、延迟、副本、索引、缓存
│   ├── test_security.py              # 订单/工具/字段/令牌/路由隔离
│   ├── test_contracts.py             # 整数金额、Enum、带时区时间
│   ├── test_persistence.py           # 持久化、审计与数据库重开
│   └── support/inspector.py          # 仅测试使用的真值读取器
└── docs/simulator.md
```

`.local/` 保存本机数据库、凭证和测试文件，已加入 `.gitignore`。场景定义是可信服务端代码，不下发给工具调用方。当前用 Python 构建类型化 fixture，以便 S6 实际执行旧解析器；后续可以在保持同一契约的前提下提取为 JSON fixture。

## World State 与 Observation

`WorldState` 是某个模拟业务时点的事实，包含 AssetSystem、GuaranteeCore、FundSystem、CallbackGateway、MessageSystem、AccountingSystem、ProtocolRegistry，并补充请求 Trace 和资产通知投递记录。

`Observation` 是某个工具在 `observed_at` 能够查询到的授权投影。工具先选择主视图/副本/缓存，再按字段白名单投影，不会直接序列化整个 WorldState。工具调用只写观测审计记录，不改变金融事实。

例如 S6 中，实际 World 包含已结算的支付记录、已验签的回调、DLQ 解析错误，以及 `expected_entry`。但：

- `get_fund_order` 只能返回资金订单、状态、借据号和金额，不能顺带暴露支付终态。
- `get_payment_transaction` 才能读取支付记录和 `payment_finality`。
- `get_callback_gateway` 返回接收/验签元数据；原文需要单独的 `get_callback_raw` 权限。
- `get_messages` 提供消费状态、DLQ 与错误，不绕过原文权限泄露消息正文。
- `get_accounting` 只返回实际记录，不返回用于测试判定的 `expected_entry`。
- ground truth、场景编号、根因标签、故障注入策略不进入任何 Observation。

### 时间与金额

所有业务事件使用带时区的 `event_time`。Observation 总是包含带时区的 `observed_at`，另外有 `source_as_of` 描述读视图的时点。种子从 `2026-09-10T09:30:00+08:00` 开始，按业务事件形成不可变历史快照；默认观测时钟是其后 10 秒。

超时或没有可见事件时，`event_time=null`，因为工具没有取得事件时点。不得拿服务器知道的真实事件时间填充失败响应。空列表/空账务记录也可以没有事件时间，但总有明确的观测时间。协议另外保存 `effective_time`；查询拒绝未来时间，支持显式读取旧版本进行比较。

全部 `amount` 单位为**分**，`2_000_000` 表示 20,000 元。Pydantic 严格整数校验拒绝浮点数、数字字符串、布尔值和负数。货币、业务状态、支付终态、Callback/消费/投递状态、观测结果及故障种类均使用 Enum。

### 状态语义

| 字段 | 含义 |
|---|---|
| `status=OK` | 成功取得允许的投影，可能是空列表；不是放款成功 |
| `status=TIMEOUT` | 本次工具读取超时，没有业务事实返回 |
| `status=NOT_FOUND` | 当前工具视图没有匹配数据；不是资金未执行的证明 |
| `knowledge=OBSERVED` | 有一份可见数据；不是完整资金结论 |
| `knowledge=UNKNOWN` | 本次读取不能提供目标事实 |
| `payment_finality=UNKNOWN/PENDING/NOT_EXECUTED/SETTLED` | 资金源明确提供的模拟付款状态，与传输状态分离 |
| `freshness` | CURRENT / STALE / UNKNOWN，指查询视图的新鲜度 |
| `completeness` | COMPLETE / PARTIAL / UNKNOWN，仅针对该工具的字段与查询范围 |

例如 S8 的资金订单工具返回旧缓存中的 NOT_FOUND、支付工具超时，所以资金事实仍须 UNKNOWN。测试 oracle 知道该请求实际处于 PENDING，但不会将 PENDING 偷渡到超时响应。这里没有实现一个替 Agent 下结论的推理器。

## 八个场景

S 编号是本阶段世界场景；原 README 的 D 编号是未来端到端演示，不应混为一套编号。

| 场景 | 真实事实 | 初始可见线索 |
|---|---|---|
| S1 请求根本没发送 | 有原放款意图，但未出站，资金方无受理、无支付 | Trace `sent=false`；资金查询 NOT_FOUND/UNKNOWN |
| S2 已发送，资金方未受理 | Trace 已发送，资金方尚无受理 | 原请求 Timeout；资金查询 NOT_FOUND，不能据此新建请求 |
| S3 明确失败 | 资金方受理后明确拒绝，没有放款 | 资金状态 FAILED；支付终态 NOT_EXECUTED；响应明确失败 |
| S4 已放款，HTTP Response 丢失 | 放款及后续回调处理实际完成 | 原请求 Timeout；资金支付 SETTLED；担保方副本初始仍 PROCESSING，随后追上 |
| S5 已放款，Callback 未到 | 放款 SETTLED，网关没有原回调 | 资金成功、网关未查到、消息列表空、本地未入账 |
| S6 网关已收，解析失败 | v2.3 字符串 loanNo 被旧整数解析器拒绝，进入 DLQ | 原请求 Timeout；网关验签通过；MQ 解析错误；我方/资产方 PROCESSING；实际账务空 |
| S7 我方成功，资产通知失败 | 回调已消费、我方及账务已完成、资产方仍处理中 | 资产通知 FAILED，其他业务事实已完成 |
| S8 资金暂不可确认 | 资金方实际处于 PENDING，暂无结算交易 | 原请求 Timeout、资金旧缓存无结果、支付查询持续 Timeout，必须 UNKNOWN |

所有场景使用同一业务订单号 `JD202609100001`，以随机 `simulation_id` 隔离世界。编号不编码进业务 ID，访问令牌在服务端绑定到对应世界，因此相同订单号不会跨世界串查。新建场景是操作员初始化测试世界，不是 Agent 创建放款意图。

S6 的业务时序：

```text
09:30:00  本地订单 PROCESSING，version=17，存在原放款意图
09:30:01  请求出站；资金方受理
09:30:02  本金 2,000,000 分完成模拟放款，loanNo=LN-20260910-001
09:30:03  原 HTTP 请求读取超时；Callback 到达 Gateway 并验签成功
09:30:04  旧 Consumer 的 StrictInt 校验拒绝字符串 loanNo；消息进入 DLQ
09:30:05  我方仍 PROCESSING，Callback 为 PARSE_FAILED，实际账务没有记录
09:30:10  调查工具开始获取 Observation
```

业务记录的时间是事件发生时间；请求 Trace 在超时结束时记录此次调用摘要，并保留 `sent_at`。旧版协议 2.2 的 loanNo 是 integer；2.3 是 string。两版都明确订单 SUCCESS 表示借据业务结果，真实付款还要查支付记录。

`signature_verified=true` 是 Simulator 的预置验证事实，本阶段没有实现真实金融签名 PKI。`expected_entry` 是模拟担保业务投影要求，不是凭空在担保方生成资金方贷款资产分录。

## 观测故障

可信操作员或测试可添加 `ObservationFault`，限定一个工具、开始时间和可选结束时间。API 调用方无权设置故障或推进时间。

| 故障 | 实现行为 |
|---|---|
| TIMEOUT | 返回无 data、无 event_time 的 TIMEOUT/UNKNOWN；不改变世界 |
| DATA_DELAY | 在可见性窗口前不返回记录；窗口结束后原记录可见 |
| REPLICA_LAG | 读取 `observed_at-lag_seconds` 之前的实际历史快照 |
| INDEX_MISSING | 世界中记录存在，但对应索引查询返回无结果且范围不完整 |
| OLD_CACHE | 读取指定历史 revision；到期后恢复当前视图 |

故障可组合：TIMEOUT 优先于不可见，再处理缓存/副本；多个缓存策略取最旧 revision，副本策略取最大延迟。缓存存在时优先选缓存，模型不会看到用于实现这一选择的 fault 配置。重复读取不会偷偷移除故障，只有操作员推进虚拟时间跨过结束边界才恢复。

网络客户端本身发生 `httpx.TimeoutException` 与模拟 API 返回 `status=TIMEOUT` 不同：前者连 Observation 都没有获得，后者是可持久化的模拟读取结果。两者都不能当成业务 FAILED；客户端不自动重试写入，本阶段也没有任何写业务工具。

## 工具权限

HTTP 只有 `POST /tools/{tool}`，使用 Bearer Token。令牌在数据库中仅保存 SHA-256 摘要，绑定世界、订单、允许工具及虚拟时间有效期。支持可信操作员撤销。API 不接受调用方传入 role、simulation_id、数据库查询、字段白名单、observed_at 或故障配置。

| 工具 | 可见内容 |
|---|---|
| get_asset_order | AssetSystem 的业务字段 |
| get_guarantee_order | GuaranteeCore 的业务字段及订单适用协议 |
| get_fund_order | FundRecord；不带支付终态及受理内部标记 |
| get_payment_transaction | PaymentRecord 及对应模拟交易 |
| get_loan_note | 借据号、关联请求、本金与币种 |
| get_callback_gateway | 原回调接收和验签元数据 |
| get_callback_raw | 有单独权限才返回的原始 Callback |
| get_messages | 消费状态、DLQ、结构化解析错误；不返回原消息正文 |
| get_accounting | actual_entry；无 expected_entry |
| get_request_trace | 我方可见的请求与 HTTP 结果，包含 sent_at |
| get_asset_delivery | 我方资产通知的投递状态 |
| get_protocol | 指定版本/业务时点适用的协议、字段及幂等语义 |

每次获授权的查询将完整 Observation、规范化请求及内容 Hash 追加到 `tool_observations`。Observation ID 可作为未来 Evidence Store 的原始引用。当前审计是应用级追加写入，不宣称抵抗拥有数据库管理权限的篡改者。

### ground_truth 的边界

ground truth 持久化在 `evaluation_ground_truth`，测试使用 `tests/support/inspector.py` 读取。工具服务不查询这张表；API 不提供 World、ground truth、场景管理或通用数据库接口。

**隔离以进程和权限边界为前提，Python 私有属性不是沙箱。** 未来 Agent 只能获得 HTTP ToolClient、指定订单及令牌；不能给它服务端 Python 对象、数据库凭证、源码/fixture 文件访问权或宿主机 Shell。若把 Agent 与 Simulator 放在同一拥有任意 Python/SQL/文件执行能力的进程里，本项目不声称还能阻止其反射读取真值。

本地操作员掌握初始化/时钟 CLI 和数据库，属于可信方。种子生成的凭证文件含本地管理用 simulation_id，应留在操作员侧；给工具调用方传入的只有 token 和业务订单号。模拟令牌有效期以虚拟时间计，本阶段不是通用的真实身份认证系统。

## 本地运行

需要正常的 Python 3.11+。以下为 PowerShell，从仓库根目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[test]'

# 默认文件型 SQLite：不需要 Docker，适用于功能演示。
.\.venv\Scripts\python -m credit_harness.cli init
.\.venv\Scripts\python -m credit_harness.cli seed S6 --credentials-file .local/s6.json
.\.venv\Scripts\python -m uvicorn credit_harness.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

另开终端，实际通过 HTTP 读取工具：

```powershell
.\.venv\Scripts\python scripts/inspect_scenario.py --credentials-file .local/s6.json
```

该脚本是人工检查工具，固定依次查询四个证据来源，不是 Agent，也不宣称能自主调查。要读取原始 Callback，创建场景时显式增加 `--allow-raw`；默认凭证没有原文读取权限。

也可使用 PowerShell：

```powershell
$simCredential = Get-Content .local/s6.json -Raw | ConvertFrom-Json
$simHeaders = @{ Authorization = "Bearer $($simCredential.tool_token)" }
$simQuery = @{ internal_order_id = $simCredential.order_id } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/tools/get_messages -Headers $simHeaders -ContentType application/json -Body $simQuery
```

推进观测时钟的命令只供本地操作员使用：

```powershell
$simCredential = Get-Content .local/s6.json -Raw | ConvertFrom-Json
.\.venv\Scripts\python -m credit_harness.cli advance $simCredential.simulation_id 30
```

运行 PostgreSQL 时，在初始化和启动 API 的终端都设置同一个 URL：

```powershell
docker compose up -d
$env:SIM_DATABASE_URL = 'postgresql+psycopg://sim:local-simulator-only@127.0.0.1:54329/credit_sim'
.\.venv\Scripts\python -m credit_harness.cli init
.\.venv\Scripts\python -m credit_harness.cli seed S6 --credentials-file .local/s6-pg.json
.\.venv\Scripts\python -m uvicorn credit_harness.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`.env.example` 不会自动加载；需手动设置环境变量。默认启动不创建业务数据，`init` 只创建缺失表，不清空已有世界。测试世界按随机 ID 隔离，可以保留多个场景并使用不同令牌访问。

## 测试

```powershell
.\.venv\Scripts\python -m pytest -q
```

默认在每个测试独立的文件型 SQLite 数据库中运行；PostgreSQL 专项用例在未配置 URL 时明确 skip。

```powershell
docker compose exec postgres createdb -U sim credit_sim_test
$env:TEST_POSTGRES_URL = 'postgresql+psycopg://sim:local-simulator-only@127.0.0.1:54329/credit_sim_test'
.\.venv\Scripts\python -m pytest -q --postgres
```

`--postgres` 将全部数据库 fixture 切换到 PostgreSQL，每个测试创建随机 schema，完成后只清理自己创建的 schema。独立的 reopen 测试在指定测试数据库保存并重开数据；不要将 TEST_POSTGRES_URL 指向真实业务库。这里验证的是 Simulator 事实/观测的持久化，不是尚未实现的修复 Worker Crash 恢复。

本次实现验证记录：Python 3.14.7、PostgreSQL 18.4 上全套 **60 passed，无跳过**；SQLite 首轮为 **59 passed、1 个未配置 PostgreSQL 的专项测试跳过**。另通过独立 Uvicorn 进程、真实 HTTP ToolClient 和 CLI 的 S6 冒烟验证，依赖检查及 Python 编译检查通过。测试存在两条上游 Starlette/httpx、AnyIO 弃用提示，不影响当前结果；Docker Compose 配置未在本机运行，PostgreSQL 验证使用本机独立实例。

Windows 受限环境如果 pytest 的临时目录 ACL 不可用，可选择仓库内一个**尚不存在**的 `.local/pytest-run-xxx` 作为 `--basetemp`，必要时在允许的项目测试环境中运行；不应为通过测试修改系统 Python 或现有数据库。

## 当前限制与后续接入点

- 当前是八种固定业务世界及可配置读视图，没有金融写操作、Agent 或在线决策。
- 时间推进改变观测可见性，不凭空把 PENDING 改成 SETTLED；世界事实变更需未来增加显式可信事件命令。
- SQLite 用于轻量功能验证；并发事务、隔离级别与资金修复必须在 PostgreSQL 和后续命令层验证。
- 当前 `create_all` 只用于新 Simulator schema；修改已有数据结构时应引入显式迁移，不能把建表调用当迁移工具。
- 后续 Agent 以 ToolClient/Observation DTO 对接；后续测试 Evaluator 可用 test inspector；在线 Evaluator 应基于独立查询，不把场景答案当生产证据。

实现所依赖的技术语义可查阅 [Pydantic 类型与严格校验](https://docs.pydantic.dev/latest/api/types/) 和 [SQLAlchemy JSON 类型](https://docs.sqlalchemy.org/en/20/core/type_basics.html#sqlalchemy.types.JSON)。金融协议、场景与错误码均为本项目虚构，不对应真实机构实现。
