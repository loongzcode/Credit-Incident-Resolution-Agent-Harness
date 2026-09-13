# Step 17：部署与运行手册

当前所有数据和 Effect Adapter 都是 synthetic。这里提供可部署的运行边界和验收脚本，不代表已经连接真实银行或已经通过企业安全认证。

## 数据库升级与权限

生产进程使用 `进程专用 Settings` 创建标记为 `production_runtime` 的 PostgreSQL Engine。所有历史 `create_*_schema` 入口在该 Engine 上停止 bootstrap；启动检查 Alembic HEAD，不匹配直接拒绝。测试 fixture 保留快速建表。

迁移身份独立于 API/Worker 数据库身份：迁移身份持有 schema owner、扩展和索引管理权限；运行身份只得到其部署职责所需的表/sequence DML，不授予 schema CREATE、数据库 CREATE 或扩展管理权限。不要使用数据库超级用户运行应用。密钥和数据库 URL 通过 Secret Manager 注入，不写镜像、仓库或命令参数。

版本链：

| 版本 | 内容 |
| --- | --- |
| `0016_baseline` | 固定的 Step 16 全表 DDL；PostgreSQL 安装 vector；SpaceHead 初始行 |
| `0017_expand` | 共享 Frame、Worker 心跳、迁移审计、检索 generation/completeness、Space 操作审计；nullable projection_version 与索引 |
| `0017_backfill` | 只回填旧 Trace 版本，不改写 payload；主表与向量表变更触发 generation 失效 |
| `0017_contract` | 激活 projection_version 非空约束，写入迁移审计 |
| `0017_1_metrics` | 新增 retrieval count/sum 聚合表 |

迁移不是应用启动任务。先在备份恢复的副本中演练，再由单个迁移作业执行：

```sh
python -m alembic upgrade 0017_expand
python -m alembic upgrade 0017_backfill
python -m alembic upgrade head
```

使用 `MIGRATION_DATABASE_URL`；PG lock timeout 为 5 秒、statement timeout 为 120 秒。失败事务回滚，排查后重新执行；不能盲目 stamp。大规模上线时回填需要拆成可重入批次后再 contract，本次数据量下采用有界超时事务。已有未版本化 Step 16 数据库应先备份，在副本比对 `0016_baseline` 的全部表、列、约束和索引，审核通过后由迁移管理员显式 `alembic stamp 0016_baseline` 并保留变更工单，再跑升级。当前不提供自动推测旧库版本的入口。版本审计表记录成功阶段；外部变更系统保存执行者、镜像 SHA 和工单。

迁移为 forward-only。失败优先向前修复；需要恢复备份时先停止所有写入，避免恢复旧库后又接收旧 Worker 的提交。扩展阶段可部署旧版应用；contract 后应使用通过兼容性演练的版本，不承诺任意历史镜像可直接回退。

HNSW 由 index administration 创建，运行进程不能 `create_space`。正式管理命令：

```sh
python -m scripts.index_memory prepare --production --tenant TENANT --provider openai
python -m scripts.index_memory activate --production --tenant TENANT --space-id SPACE
python -m scripts.index_memory rollback --production --tenant TENANT --space-id OLD_SPACE --provider openai --actor APPROVED_ADMIN
```

这些命令使用迁移/索引管理身份。先准备匹配 provider/model/dimension 的 BUILDING Space，等待全部租户回填完成，再激活。HNSW 创建在空的 BUILDING 分区上进行；不在请求路径创建，不用阻塞式重建已在线索引。大规模既有索引替换应使用单独 DBA 作业的 `CREATE INDEX CONCURRENTLY`。

参考：[Alembic 升级实践](https://alembic.sqlalchemy.org/en/latest/cookbook.html)、[PostgreSQL 索引并发构建](https://www.postgresql.org/docs/18/sql-createindex.html)。

## API、多实例和身份

生产入口：`credit_harness.production.app:create_app`，Uvicorn `--factory --no-proxy-headers --no-access-log`。独立进程命令见 Dockerfile。API 没有 SimulatorAdmin、业务写入口或上游 Tool credential。

OIDC Access Token 必须通过 RS256 签名、issuer、audience、有效期和 `case.investigate` scope 检查。部署配置 `OIDC_GROUP_ROLES` 把企业 group 映射到应用角色；此处不建立本地密码。Registry Admin 使用另一个 audience 和 `registry.admin` scope。Admin 的生产脚本也使用迁移后的 Engine。

| 角色 | 权限 | 可见内容 |
| --- | --- | --- |
| VIEWER | CASE_VIEW | `/summary`：Case ID、状态和更新时间，无金融字段 |
| INVESTIGATOR | CASE_VIEW + CASE_TRACE_VIEW | 已授权 Frame 的 Planner、Tool、Work、Knowledge、Registry/Route 分页 |
| FINANCIAL_REVIEWER | CASE_VIEW + CASE_FINANCIAL_VIEW | 已授权 Frame 的 Evidence 分页和详情 |
| SUPERVISOR | 三项全部 | 完整 Frame、跨区 Timeline、Effect、Recovery、Evaluation、Closure |

完整 Frame 仍需要三项权限；部分权限角色可以读取同租户中已创建 Frame 的授权 section，知道 frame_id 并不授予权限。部署 SSO shell 在 React 初始化前提供 `window.investigationIdentity.getAccessToken()`，只返回内存中的短期 Access Token；不使用 localStorage。现有本地开发代理仍用于 synthetic 调试。生产镜像不内置 IdP 登录客户端或密钥；接入企业 SSO 时需通过组织的认证集成验收。

Frame 仅存储 Step 16 allowlist 投影。JSONB 附带内容指纹、租户和 Case 绑定、固定版本及 TTL。多个副本用相同 watermark/时间窗生成同一个 frame_id；第一个写入者决定 assembled_at 和 TTL，后续不能覆盖或续期。分页只读该不可变 Frame，过期返回 FRAME_EXPIRED，数据变化返回 FRAME_STALE。相同 watermark 跨 TTL 窗口产生新的缓存身份，金融数据的 watermark 不因缓存重建变化。20 并发请求的返回内容必须完全一致。

`FRAME_CURSOR_HMAC_KEY`、`FRAME_CURSOR_PREVIOUS_KEY`、`IDENTITY_ALIAS_HMAC_KEY` 为 Base64 编码、至少 32 字节的随机密钥。游标轮换先把旧 key 放入 previous，再滚动部署新 current；全部实例完成滚动并等待最长 Frame TTL 后撤掉 previous。禁止把 alias key 与 cursor key 复用。alias key 轮换会改变显示别名及新 Frame 身份，旧缓存最多保留一个 TTL；同租户同类型同 ref 稳定，不同租户不同别名。

## Trace 和 Closure

已完成 Planner Turn 的安全投影与 Checkpoint 同事务持久化，不等待 Run 最后的 append。Decision/Turn/Guidance/同一 Retrieval telemetry 重复写入是幂等的；完整 Run 内存副本在生产关闭。没有模型调用前伪造 Trace，也不存私有 CoT、Prompt 或原始 Tool Response。

显式 `trace_trust_class` 区分当前操作、组织知识、已验证历史经验、历史评估、当前 Closure 绑定评估。旧 historical 字段只为兼容保留，前端不再用它决定知识类别。检索计时/引用汇总是操作记录；真正的 Skill 为组织知识，Experience 为历史指导。

Trace projection v2 写入 tenant HMAC 别名。v1 已保存的 SHA 别名无法反推出原值，读取时使用独立 legacy 域重新 HMAC，避免继续展示可跨租户关联的旧摘要；原始审计行不改写。旧/新显示别名不作为关联业务事实的 witness。未知 projection version 拒绝展示。

展示 Closure 前重新计算原有 closure identity，并验证全部 Case/Run/Report/Snapshot ID、Evidence/Ledger/Call 指纹、策略版本、合约版本和关闭时 Case revision。只有合法 Closure 绑定的那个 PASS Report 才显示为当前结案评估；Case 状态字符串和任意 PASS 不构成结案证明。

生产 OpenAPI 来自 `create_production_ui_app`，旧 `/evidence`、`/hypotheses`、`/reasoning-context` 仅保存在 `openapi-legacy.json`。生成类型与静态检查分别比较两个契约，禁止把 compatibility API 当生产接口。

## Worker 拓扑与恢复

```sh
python -m credit_harness.production.workers agent
python -m credit_harness.production.workers orchestration
python -m credit_harness.production.workers recovery
python -m credit_harness.production.workers embedding
```

Agent Worker 消费已获准调查的 INVESTIGATION_RESUME/OPERATOR_FOLLOWUP；Orchestration Worker 消费 VERIFICATION_REQUIRED；Recovery Worker 修复 Effect 到 Work 的持久化窗口并处理 RECOVERY_RECHECK。仍由既有受控 Case 接入流程创建任务，不扫描 NEW Case 擅自开始调查。各自调用既有 claim/fence/renew/预算检查，部署不新增业务 Tool 或新金融 Effect。当前恢复适配器只查询 synthetic effect ledger，未接真实企业副作用系统。

Embedding Worker 周期扫描已提交的 Skill/Experience，enqueue durable IndexJob，按租约 claim；网络调用在数据库事务外。`RECONCILE_SECONDS` 可配置，默认 60 秒；失败按既有退避重试。BUILDING/ACTIVE Space 的 provider contract 不匹配时交给对应模型版本的 Worker，不混用向量。应用的 Guidance 使用该索引，失败时降级，不影响金融事实来源。

所有 Worker 在 SIGTERM/SIGINT 后停止下一次 claim，当前 tick 最多等待配置的 grace；超时退出进程，未完成租约到期由其他实例接管。不会在 shutdown 时重新 dispatch Effect。部署停止宽限应大于应用 grace；容器默认 135 秒覆盖最大 120 秒应用 grace。HTTP/SQL timeout 有限，worker lease 必须大于 IO 超时与提交余量。

`TOOL_BINDINGS_FILE` 是可信部署侧的只读挂载 JSON：system_id、adapter_id、query_version、response_version、HTTPS base_url、每个已授权 Case 的上游 credential。不可挂在前端目录，不由 Agent/API 提交。后续生产接真实系统时用企业 Credential Broker 替换此 synthetic 清单，不把凭据写入 Registry。

## 向量完整度与回滚

Top-K 每种 DocumentType 一次主表 batch load；逐条验证 hash/status/scope/source Case。向量不决定证据、能力或授权。

主表和索引表 INSERT/UPDATE/DELETE 的数据库 trigger 推进 tenant/document_type generation。Reconcile 在一致快照中扫描完整度并保存 indexed_generation。检索只做四个主键读取比较两类 generation，不做全表完整度 COUNT。任何 generation 变化立即 fail closed，直到下一次回填与 reconcile。检索自身的租户/范围过滤计数仍用于 telemetry，与完整度扫描不同。

回滚只接受 RETIRED Space，验证 provider/model/dimension/contract 和所有租户的主表 hash 关联完整度，锁全局 SpaceHead，原子切换 ACTIVE 并写审计。生产中管理员还需在同一变更窗口协调各租户对应旧模型 Worker，并等待新 active Space 的 completeness 就绪。向量质量差不授权绕过这些检查。

## Health、监控和日志

`/health/live` 仅表示进程存活。`/health/ready` 验证 DB、精确迁移 HEAD、可验证 Registry head 及配置中 required_workers 的新鲜心跳。Frame keys 在启动时校验。Embedding 是 optional guidance，不列入默认 critical workers；可以按部署要求显式加入。

`/metrics` 是内网聚合端点，反向代理不公开。包括 Agent run、Planner failure、Tool call、WAITING/ESCALATED、UNKNOWN Effect、Recovery attempt、Evaluation verdict、Frame stale/latency、Retrieval latency、IndexJob pending/failed。当前数据库计数属于 gauge，即使名称沿用 `_total`；不是跨删除永久单调的 counter。Frame 计时为实例本地值，由监控系统聚合。没有 Case/订单/客户/交易/合作方 ID label。

日志仅允许 event/component/severity/error_code/duration/request_id；不格式化外部异常消息、HTTP URL/参数、凭据、Evidence、callback、embedding。生产关闭 Uvicorn access log；Nginx 不记录请求路径。当前没有 OpenTelemetry，避免无审查的自动 instrumentation 捕获请求 body。后续引入时只允许 operation span 和同样的安全字段。

默认池大小 10、最大 overflow 0、pool timeout 5 秒、recycle 900 秒；PG statement timeout 5 秒、lock timeout 5 秒、idle transaction timeout 10 秒，pgvector 检索另限 3 秒。读失败不能写 FAILED 业务事实；UNKNOWN 继续保留。

## 锁顺序

1. Case 行锁先于该 Case 的 Call/Evidence、Work/Resume、Effect/Recovery、Report/Closure 写入。
2. 路由/调查 admission 如需同时访问 Registry，则 Case 后取得 RegistryHead；Registry Admin 只锁 RegistryHead，不反向取得 Case。
3. Work claim/renew 与 Recovery claim 都先锁 Case，再验证自身 lease/fence。租约不跨网络持有 SQL 事务。
4. Embedding 使用 IndexJob CAS → 主表只读 → 向量 upsert → generation；Space 管理锁 SpaceHead。Reconcile 使用 REPEATABLE READ 和 completeness 写入，不反向锁 Case。
5. Frame 是 REPEATABLE READ + 提交后 watermark 检查，不取得业务写锁；缓存事务独立。

新增 PostgreSQL 并发 Evaluator/Closure 测试验证同一 Case 四路竞争只有一个 Closure identity，无死锁；既有路由、恢复、Work、Effect 并发测试继续跑全量。死锁/序列化失败必须回滚，不能在事务未知结果后自动重放金融副作用。

## 备份、故障和保留

`test_synthetic_backup_restore_and_vector_rebuild` 在 PostgreSQL 使用真实 pg_dump/pg_restore 到新生成的隔离测试数据库；SQLite 使用在线 backup API。验证所有现存业务表内容指纹一致，包括 Case、Evidence、Registry、Effect Ledger、Recovery、Closure、Memory；删除两类向量后重新处理 IndexJob，业务指纹不变。测试只使用 synthetic 数据。正式备份需要组织级加密、访问控制、PITR/WAL 归档和异地恢复；这些设施未在本机搭建。

故障覆盖：断开 DB、完成 Turn 后 crash、Embedding timeout、Registry 缺失/篡改、Frame TTL、Worker 停机、租约过期、并发评估/关闭。既有 financial safety tests 继续负责不重复 Effect、不伪造 Evidence、不把 UNKNOWN 升级为 FAILED。

| 数据 | 保留策略 |
| --- | --- |
| Frame | 默认 300 秒；API lifespan 每 60 秒清理过期行；分页不续期 |
| Safe Trace / Planner audit | 默认保留 90 天在线，随后按企业审核归档；本阶段不自动删除金融调查审计 |
| IndexJob | active/failed 保留用于恢复；completed 可在 30 天后经管理作业归档，但须能通过 reconcile 重建 |
| Evidence / Closure / Effect | 遵从企业业务主数据策略，Step 17 不自动删除 |

90/30 天是部署基线，不是法律期限；归档需要业务、审计与数据负责人确定，当前只执行 Frame TTL 清理。

## 容器与发布门禁

Dockerfile 的 api/worker target 使用同一 runtime 层；镜像 label 和 tag 绑定 RELEASE_SHA。前端多阶段构建后由 Nginx 提供静态资源，不运行 Vite dev server。外部只开放 HTTPS；HSTS、CSP、nosniff、Referrer-Policy、大小和超时限制在代理配置中。不信任客户端 X-Forwarded-*。Ant Design 的运行时样式需要 style-src unsafe-inline；script-src 不允许 unsafe-inline。企业 SSO 如需跨域，仅加入经过批准的具体 origin，不使用通配符。

Compose 将 Registry Admin 作为单独进程复用 API 镜像，`/admin-api/` 代理到该进程的 `/admin/`，与调查 `/ui/` 分开。`ADMIN_ENV_FILE` 单独注入 Admin 的数据库身份及 `REGISTRY_GROUP_ROLE_MAPPING`；audience 必须采用中央配置的 `REGISTRY_ADMIN_AUDIENCE`。此进程不挂载 Tool credential。Excel 请求限制为 6 MB，调查 API 为 1 MB。

CI 在同一个 checkout 运行语义 lint、AST/契约检查、SQLite full、PostgreSQL+pgvector full（含迁移 fresh/upgrade 和备份恢复）、前端测试、TypeScript/Vite build，再构建三个镜像。语义 lint 检查无效控制流、tuple assert 和重复字面量 key；不是完整类型检查器。真正 LLM/Embedding 和本地保密清单测试保持 opt-in。

```sh
git status --porcelain  # 必须为空
python -m scripts.verify_release --workers 4
```

输出 `.local/release/<SHA>/release-verification.json`，记录实际退出码、JUnit passed/skipped/failed、日志 SHA256、迁移 revision 和时间。脚本在每项测试前后检查 clean HEAD，任何改动或漏跑都不能产生 passed=true。该报告是 CI artifact，不反向提交到同一个 SHA；否则提交本身会改变被验证的 SHA。源码变化必须重新提交并完整重跑。

没有 Docker 时使用 --local-tests-only；完整发布报告还必须包含三个镜像构建通过。依赖固定在 requirements.lock 与 npm package-lock.json；后续升级依赖同样走完整门禁。

## Step 17.1：进程隔离

DatabaseSettings 共享 PostgreSQL/tenant/pool/timeout 验证，Settings 共享环境变量解析。进程模型 extra=forbid，装配入口要求与 kind 完全一致的配置类型。

| 进程 | 配置模型 | 专有必需配置 |
| --- | --- | --- |
| API | InvestigationApiSettings | Investigation OIDC/group roles、Frame HMAC、Alias HMAC、ALLOWED_ORIGINS |
| Admin | RegistryAdminSettings | issuer/JWKS、REGISTRY_ADMIN_AUDIENCE、REGISTRY_GROUP_ROLE_MAPPING、ADMIN_ALLOWED_ORIGINS |
| Agent | AgentWorkerSettings | PLANNER_MODEL、OPENAI_API_KEY、Alias HMAC、Tool binding |
| Orchestration | OrchestrationWorkerSettings | Tool binding、Work lease/timeout |
| Recovery | RecoveryWorkerSettings | 数据库、Work lease/timeout |
| Embedding | EmbeddingWorkerSettings | OpenAI key、embedding model/dimension、lease/reconcile |

API/Admin 不接受 capability signing、Planner、Embedding 或 Tool binding。Embedding 不接受 Planner、OIDC、Frame/Alias、Capability、Tool binding。六个进程都没有签发/验证写 Capability 的职责，因此均不注入 CAPABILITY_SIGNING_SECRET。Recovery 只使用账本身份做 lookup，不重新签发 Capability。

Recovery 没有 Planner、Embedding、Hybrid Retrieval、Agent runtime 或 Read Executor；其 Evaluator 仅计算恢复进度。Orchestration 只接受 VERIFICATION_REQUIRED，无 Agent runtime。INVESTIGATION_RESUME/OPERATOR_FOLLOWUP 由 Agent Worker 接管。Orchestrator 在处理前检查 WorkType，误路由直接拒绝，保持 effect-bound recovery fence。

Agent 的 Planner 必需配置与可选 Embedding 分开解析。可选配置缺失、格式不合法或 provider 构造失败返回 RETRIEVAL_FAILED/no guidance；运行时检索失败沿用既有安全降级。不伪造经验、不改变金融状态或硬策略。Provider 调用有 timeout，异常正文不外泄。

### 配置与 audience 部署门禁

使用 deploy 下六份独立 env.example，只允许 literal KEY=value（JSON 字段保留 JSON），不做 shell 执行或变量插值。Secret Manager 生成对应进程专用文件，路径通过 API_ENV_FILE、ADMIN_ENV_FILE、AGENT_ENV_FILE、ORCHESTRATION_ENV_FILE、RECOVERY_ENV_FILE、EMBEDDING_ENV_FILE 提供。

支持的部署入口：先运行 python -m scripts.deploy_production --check-only，再运行 python -m scripts.deploy_production。

入口验证必需字段，拒绝每份文件中不属于该进程的字段，同时比较 API/Admin audience，要求不同。独立进程不能在不共享部署信息的情况下判断对方 audience；跨进程不变量由部署入口检查，两边仍各自验证 token audience/scope。不要绕过预检直接 docker compose up。控制平面持有配置文件，API/Admin 不互相接收 Secret。

Compose 仅给 Agent/Orchestration 挂 Tool binding；OpenAI key 只属于 Agent/Embedding。Alias HMAC 用于 Agent 安全 Trace 和 API 展示，不是 Capability。共享镜像代码不代表共享凭证。

### 历史审计和指标

0017_backfill 不再 UPDATE payload，只标识 projection_version=1。stored_trace 在内存中根据 kind/status/historical 推导信任分类；真正旧版 kind=Evaluation、historical=true 显示 HISTORICAL_EVALUATION。旧 SHA 别名读取时重新 HMAC，不覆写原文。已经执行过旧版 payload 改写的数据库无法凭空恢复原字节，需要原备份；补丁保证从原始 0016 基线升级不再改写历史。

检索记录和 retrieval_metrics 累加在同一事务。只有 Trace INSERT RETURNING 证明首次插入，才对同租户 count/sum 原子 upsert；重复提交不重复计数，回滚不留孤立指标。scrape 通过租户主键读取一行，不读 Knowledge Trace payload。聚合从新版本启用开始，不扫描历史；它是运行指标，不是业务证据。

### 完整发布证明

verify_release 默认运行 static、lint、SQLite full、PostgreSQL+pgvector full、frontend full、TypeScript/Vite、API/Worker/Frontend Docker build。三个构建使用同 HEAD revision label/tag；镜像失败或 Docker 不可用时 passed=false。每项检查前及最终核对 clean HEAD，结果写入 artifact，包含 GitHub run ID。

没有 Docker 时只能 --local-tests-only，生成 local-verification.json，其中 passed=false，不得冒充完整发布证明。

main ruleset 要求 PR、required check release-candidate / verify、strict/up-to-date、禁止 force push/删除。是否启用以 GitHub 查询为准，YAML 不代表远端规则生效。最终报告链接真实 Actions run 与同 SHA artifact，不能沿用旧计数。
