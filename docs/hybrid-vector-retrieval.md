# Hybrid Vector Retrieval — Step 15

规则决定哪些知识有资格出现，向量只负责在合格知识中寻找相似内容。Skill 与已验证历史经验仍然是调查建议；相似度不能证明当前订单付款、满足 Gap、确认 Hypothesis、授权副作用或关闭 Case。

## 为什么使用 PostgreSQL + pgvector

项目已经用 PostgreSQL 保存业务记录。把可重建搜索索引放在同一数据库，可以直接关联主表状态，避免为了检索再维护一套独立数据库。`organizational_skills` 与 `verified_incident_experiences` 是主数据，向量表删除后可以重建。没有引入 Milvus、Qdrant、RAG 平台或另一套业务真值。

PostgreSQL 必须安装 pgvector。`create_retrieval_schema(engine)` 执行 `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public`，创建独立 retrieval metadata 中的表。Docker Compose 使用官方 `pgvector/pgvector:pg17` 镜像；已有 Windows PostgreSQL 需事先安装扩展。生产部署应由迁移身份安装扩展与建索引，查询身份无需这些权限。

| 表 | 内容与约束 |
|---|---|
| `retrieval_embedding_spaces` | Provider、模型、维数、投影/归一化/接口版本、时间、BUILDING/ACTIVE/RETIRED；唯一部分索引保证最多一个 ACTIVE |
| `retrieval_space_head` | 单行 ACTIVE 指针，事务内切换 |
| `skill_vector_index` | tenant + Skill ID/version + space 唯一；source hash、projection hash、结构化 scope、真实 vector、indexed_at |
| `experience_vector_index` | tenant + Experience ID/schema version + space 唯一；同上，并有服务器私有 source Case 关联用于排除当前 Case |
| `retrieval_embedding_index_jobs` | 内容身份、状态、attempt、lease token/期限、next_eligible_at；唯一内容作业身份 |

向量列是 PostgreSQL `vector`，不是 JSON 中保存数组再用 Python 算距离。每个空间建立 `embedding::vector(dimension)` 的部分 HNSW cosine 索引，避免不同维数共用同一索引。tenant/status/space/domain/partner/product 有 B-tree 索引。查询执行 `<=>`、SQL WHERE/JOIN 与 LIMIT 20；启用 pgvector 0.8 的 iterative scan。过滤很强或数据少时 PostgreSQL 可以选择顺序扫描并在 SQL 内计算精确距离；“存在 HNSW”不等于声称每次查询一定使用 ANN。

SQLite 明确使用 `ExactVectorTestRepository`，JSON 向量和 Python 精确 cosine 只用于离线单元测试。生产 `PgVectorRepository` 拒绝 SQLite，测试替身拒绝 PostgreSQL；生产失败不会偷偷改用全表 Python 搜索。

## 哪些内容可以进入 Embedding

`projection.py` 从严格模型生成 `SemanticProjection`，再用静态词汇模板生成文本。不是完整模型 JSON 拼接，也没有可接受任意文本的 Runtime 检索入口。

| 来源 | 允许的投影 |
|---|---|
| ACTIVE Skill | 调查目标、枚举症状、已知模式、推荐 Claim 类型、反模式/安全经验 |
| ACTIVE Verified Experience | 枚举化事故状态、实际观测 Claim 类型、Tool 类型顺序、验证结果路径、安全经验 |
| 当前查询 | 经过完整性检查的 `ReasoningContextSnapshot` 中 typed state、UNKNOWN/MATCH/MISMATCH、当前 Gap 所需 Claim/Requirement 类型 |

Raw Evidence、Callback 原文、原 Tool Response、订单/交易/借据/消息 ID、姓名、身份证、卡号、手机号、身份 token、Evidence ID、审批人员、Capability、Ledger、Recovery proof、私有推理均不进入 Embedding 文本。经验的 source Case/closure/report/hash 也不进入文本。公司 Excel 与 System Registry 描述完全不向量化。

`RetrievalIncidentState` 独立列明字段，未来向 IncidentSignature 增加字段不会自动扩大 Embedding 资格。Context 中允许的带名称 release/version 不能直接转入 Embedding；这里只接受有界数字版本。

SourceDocument 中的服务器关联 ID 与 scope 只用于索引绑定/过滤，不传给 Provider。即使身份 token 在未来 Planner Context 中被允许，检索 Embedding 也不需要它，因此这里继续移除。

Provider 调用之前先重新 Pydantic 校验，再限制状态为 Enum/bool/结构化版本，拒绝外部标识字段，最后用数字 PII 扫描和 16,000 字符限制做第二道检查。自由姓名或 credential 字段根本无法由这个 DTO 表达；不能把“扫描器识别所有姓名”作为保证。修改词汇投影必须代码审阅并升级 projection version。

查询向量只存在于一次调用内，不建 current_case_embedding_history，不保存查询文本或数组到日志和审计。

## Hard Filter：先决定资格

`MemorySources` 从主表读取并验证不可变内容哈希。查询从 tenant-scoped Case 做 admission，Registry 配置时从 `CaseRouteContext` 获取 domain、环境、三个 partner role、product、protocol。Snapshot 已绑定 Capability Snapshot 时再检查 route fingerprint，旧路由 Snapshot 不继续检索。没有 Registry 的旧 synthetic fixture 只能使用已知 typed scope，不推测合作方或协议。

Skill 的明确 scope 字段必须匹配；未填写表示经可信管理配置的通用 Skill。Experience 遇到当前已知而历史缺失的 partner/domain/environment/product 字段时拒绝使用，不能把历史“未知”当作跨合作方通用。协议明确不匹配排除；当前协议 UNKNOWN 时只允许不限定协议的文档，绝不从历史推断当前版本。

过滤包括：tenant、当前主表 ACTIVE、业务 scope、适用开始/结束时间，Experience 还排除当前 source Case。`VerifiedExperiencePublisher(repository, registry=registry)` 可以在首次发布时把可信 route scope 固化到主经验中；不会用今天变更后的路由重新解释旧经验。旧没有完整 scope 的经验保持未知，可以重新审阅发布新版本，不能在索引层补造身份。

主表 status、source hash 和 Experience source Case 绑定在 SQL JOIN 中检查。最终候选再次读取主表，校验 projection hash，并重新按主记录的 tenant、scope、协议、适用时间及 source Case 判断资格。即使可重建索引的合作方或 source Case 字段损坏，也只能降级为无 Guidance，不能扩张知识适用范围。旧索引中的 `source_status=ACTIVE` 不能复活 RETIRED Skill 或 REVOKED Experience。数据库写身份仍为可信边界；哈希不是防管理员篡改的签名。

## 召回、重排、最终 Guidance

```text
Snapshot → typed query → privacy validation → ephemeral query embedding
                         ↓
tenant/current primary status/scope/time filter
                         ↓
one ACTIVE space → pgvector cosine Top 20 per document type
                         ↓
deterministic business rerank → <=4 Skill / <=3 Experience
                         ↓
SkillComposer + existing <=8 strategy / <=12000 character budget
                         ↓
InvestigationGuidanceBundle → existing Planner injection seam
```

`StructuredExperienceReranker` 保留原离散 overlap：Timeout/schema mismatch 匹配权重 3，其余匹配 1；UNKNOWN 不增加 overlap。旧 `VerifiedExperienceRetriever` 保留为显式结构化基线，便于历史测试/对照。配置 Hybrid 的路径在 pgvector 失败时返回无指导，不隐式降回旧全量扫描。

重排用可解释的字典序：安全 Gap Claim 覆盖、全部 Gap 覆盖、结构化 overlap、scope specificity、当前可查询 Claim 覆盖、安全规则相关性、cosine distance，最后 document ID/version 稳定打破平局。风险不是向量分数；相似度没有任何概率含义。Top 20 只是候选集合，最终文档有独立数量和字符上限。保留 SkillComposer 对全部被选 Skill 的安全组合校验。

历史区仍明确写 `HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE`。模型可以据此建议先查 PAYMENT；当前 PAYMENT 明确为 NOT_EXECUTED 时，历史 SETTLED 不修改当前事实。向量和 Guidance 都没有 Evidence publication、授权、执行或 Closure 依赖。

## 失败与强制安全基线

无 ACTIVE space、Provider Timeout、数据库不可用、Provider/space 不匹配、缺索引、内容哈希变化都返回 `RETRIEVAL_FAILED`，bundle=None。损坏的 Skill 同样不能进入模型。Planner 的既有静态安全契约、确定性 Policy、Identity Contract 和 Evaluator 继续存在。

以下固定规则不靠向量召回：

- `NO_NEW_DISBURSEMENT_WHILE_PAYMENT_UNKNOWN`
- `HISTORICAL_EXPERIENCE_CANNOT_SATISFY_CURRENT_GAP`
- `MEMORY_CANNOT_AUTHORIZE_SIDE_EFFECT`
- `CURRENT_EVIDENCE_OVERRIDES_HISTORICAL_PATTERN`

`HybridRetrievalService.mandatory_safety` 始终暴露这份固定基线；SkillComposer 加载成功时继续合并这四条。实际业务安全仍由既有权限/身份/证据/结案代码保证，不靠这组名称或者模型服从 Prompt。

## 索引作业和模型升级

主数据事务先 commit。独立管理 Worker 调用 `reconcile(space)` 扫描 ACTIVE 主数据，为每个 tenant/type/id/source version/projection hash/space 注册幂等持久作业。该扫描也是“主表已提交但进程在 enqueue 前退出”窗口的恢复机制，不依赖提交回调一定执行。它可以定期由现有运维进程调用；本阶段不新建消息平台或自动调度服务。

`run_one(space_id)` 先通过 CAS claim，120 秒租约，随机 fencing token，随后关闭事务，才调用 Provider。完成时再次校验主表/哈希/租约，在同一事务替换索引并 COMPLETED。超时或错误进入 FAILED；next_eligible_at 从 30 秒指数退避到最多一小时，没有 sleep 重试。租约过期可回收，旧 Worker 不能覆盖新结果。Embedding 失败绝不回滚已关闭 Case。

同一 source/version/hash/space 作业完成后不重复调用 Provider；不同主记录当前分别索引，未引入跨文档 embedding cache。主内容或版本变化注册新作业；已完成作业对应的向量丢失时重新 PENDING。当前租户索引不完整会保守降级，而不是默默只返回部分历史。

新模型/维数/投影/归一化/接口版本产生新的内容寻址 EmbeddingSpace：

1. `create_space(provider, now)` 得到 BUILDING，并建立该空间 HNSW。
2. 对每个 tenant 做 reconcile/backfill。
3. `activate_space(space_id)` 锁定单行 pointer，校验所有 tenant ACTIVE 主数据均有当前 source hash 的索引。
4. 同一事务旧 ACTIVE → RETIRED、新 BUILDING → ACTIVE、切换 pointer。
5. Runtime 每次只读取一个 ACTIVE space；旧 Provider 与新 space 不匹配直接降级。

同一模型 ID 应表示固定部署版本；如果 Provider 在相同名称下更换权重，需要更改配置中的版本化 model ID/接口版本后重建。不会在同一空间原地把 1536 维换成其他维数。

## 配置与现有 Runtime 接入

```python
# 可信应用装配；这些对象和凭据不作为 Agent Tool 暴露。
sources = MemorySources(skill_repository, experience_repository, registry_repository)
vectors = PgVectorRepository(sources)
provider = OpenAIEmbeddingProvider()  # 或离线 fake
hybrid = HybridRetrievalService(vectors, provider)
planner = PlannerService(planner_model, guidance_provider=hybrid.guidance_provider())
```

先由迁移/索引进程执行 schema bootstrap、BUILDING/backfill/activation，再启动查询。现有 `PlannerService` / 调查 Runtime 使用同一 Guidance 协议，不需要更改 Tool Gateway。这里没有新增自动 Tool Call。

已经有业务主表的 PostgreSQL 可以用独立可信 CLI 管理索引；`SIM_DATABASE_URL` 留在服务器环境中。对每个 tenant 执行 prepare/work，所有 tenant 回填完整后才 activate。work 每次执行有限数量到期作业，遇到退避中的失败就返回，由下一次运维 tick 继续。

```powershell
python -m scripts.index_memory prepare --tenant demo --provider fake
# 使用 prepare 输出的 space_id；无须在命令行暴露数据库密码或 API key
python -m scripts.index_memory work --tenant demo --space-id <space_id> --provider fake --max-jobs 100
python -m scripts.index_memory activate --tenant demo --space-id <space_id>
```

真实 Adapter 位于 `adapters/openai_embedding.py`，领域只依赖 `EmbeddingProvider` Protocol。配置 `EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`OPENAI_API_KEY`；每次 HTTP Timeout 30 秒，关闭 SDK 隐式重试，由 durable job 控制重试。默认 Fake 是离线 hashed vocabulary，不能用于声称真实语义质量。SDK `httpx.MockTransport` 测试真正经过已安装 OpenAI SDK 的 Embeddings HTTP 序列化与解析。

## 遥测、实验与限制

每次检索只输出 space、各阶段数量、选中知识 ID、degradation 和各阶段耗时；不输出 query/raw Evidence/embedding 数组。可选 sink 接受严格 `RetrievalTelemetry`，sink 故障不影响调查。最终字符压缩仍由既有 Guidance 服务审计。

```powershell
# SQLite 工程测试
.venv/Scripts/python -m pytest tests/test_hybrid_retrieval.py tests/test_embedding_provider.py -q

# 指向专用测试数据库，测试创建自己的随机 schema
$env:TEST_POSTGRES_URL='postgresql+psycopg://<test-user>:<test-password>@127.0.0.1:<port>/<test-db>'
.venv/Scripts/python -m pytest --postgres -q

# 3% 同比例 synthetic 规模样本；真的通过 IndexJob 和 pgvector SQL
.venv/Scripts/python -m scripts.benchmark_retrieval --skills 30 --experiences 3000 --queries 20 --output .local/retrieval-scale.json

# 可选真实模型：外部调用会产生 Provider 费用，仅在显式配置时运行
$env:RUN_EMBEDDING_TESTS='1'
.venv/Scripts/python -m pytest tests/test_embedding_provider.py -m llm -q
```

Scale 的 corpus 是一条真实通过 synthetic Evaluator/Closure 的种子经验的明确标注扩展，不能当作数千独立金融事故样本。规模实验报告实际主表/向量/作业数量、索引耗时、查询 median/p95、重排耗时、上下文大小与稳定性；不声称百万级容量或 QPS。

脚本包含三个语义不同词的 synthetic paraphrase 对照，并报告实际期望排名。Fake 报告明确标为 `ENGINEERING_ONLY_NOT_SEMANTIC_VALIDATION`。真实 Adapter 的小样本结果也不等于企业语料质量；没有 Key 的环境跳过真实语义测试，不伪造 passed。

当前 completeness 检查使用 SQL count/join，索引构建为单 Worker tick。百万规模需要根据真实过滤分布评估 partial/partition indexes、HNSW recall/latency、批量作业与增量 completeness 标记；本次小样本不能证明这些规模上的性能。测试验证真实 `<=>` SQL 和 HNSW DDL，未把“有索引”夸大成查询一定走 HNSW。

接口依据：[OpenAI Embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create)、[pgvector 官方说明](https://github.com/pgvector/pgvector)。

本阶段没有新增 Multi-Agent、LangGraph、UI、真实资金操作、权限系统或面试包装。

## 本次可复现实测

完整 JSON 见 [synthetic scale report](examples/retrieval-scale.json)。以下数据来自最终代码运行，测量时本机同时运行全量回归。

| 项目 | 实测 |
|---|---:|
| Skill 主表 / vector | 30 / 30 |
| Experience 主表 / vector | 3000 / 3000 |
| Durable IndexJob | 3030 |
| 样本比例 | 3%（一个独立验证种子的 synthetic 扩展） |
| 构建总耗时（含 synthetic 主数据） | 120.416 s |
| 查询次数 | 20 |
| 向量阶段 median / p95 | 83.249 / 106.105 ms |
| 重排阶段 median / p95 | 141.817 / 291.863 ms |
| 总检索 median / p95 | 237.131 / 394.388 ms |
| 最大 Guidance 字符 | 10948 |
| 合格文档 / 向量候选 / 重排候选 | 2424 / 40 / 40 |
| 最终 Skill / Experience | 4 / 3 |
| 稳定选择 / 保留当前 NOT_EXECUTED | true / true |

Fake 同义样本的预期排名是 3、2、3，Recall@1=0/3；这不是通过的语义质量验收，报告明确标为仅工程测试。当前未配置真实 Provider 的 API key/model，三个真实语义测试按约定跳过，不能据此声称真实语义召回质量已达标。

本机 PostgreSQL 实测版本为 18.4，pgvector 为 0.8.1；Compose 开发配置使用 pgvector 的 PG17 镜像，本次没有把 Windows PG18 测试描述成 Docker 镜像验收。

## 最终回归结果

2026-09-13 本机运行，包含主表 scope/source Case 复核与损坏索引测试：

| 命令 | 结果 |
|---|---|
| `pytest -q -n 4`（SQLite） | **1420 passed, 8 skipped**，623.15 s |
| `pytest -q -n 4 --postgres` | **1422 passed, 6 skipped**，556.99 s |

两轮各有 5 条既有 Starlette/AnyIO deprecation warning，无失败。SQLite 多跳过两个真实 PostgreSQL 测试。共同跳过项为：三个需真实 Embedding 配置的语义测试、两个需在线 Planner/Remediation 配置的测试，以及一个本地公司文件统计测试。真实 OpenAI SDK + httpx.MockTransport 的离线 Embeddings 往返已执行通过，没有用不存在的依赖名跳过。

最终全量日志保存在本地 `.local/step15-release-sqlite.log` 与 `.local/step15-release-postgres.log`。源码/文档通过 `git diff --check`；没有更改 Financial Identity、授权、恢复、Evaluator 或 Closure 实现。
