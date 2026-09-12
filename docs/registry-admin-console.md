# Step 14.2 — Production System Registry Administration Console

管理后台建立独立的人类配置治理边界，沿用 Step 14/14.1 Registry 和 Case Route，不让 Agent 编辑平台配置。所有本地身份、系统与测试 Excel 均为 synthetic。真实公司 SSO credential、真实金融接口和真实公司工作簿未接入。

```text
公司 SSO Access Token
  → IdentityProvider 验证 → SSO groups → Application roles → Backend permissions
  → Draft Change Request → Submit → Independent Approve → Activate
  → RegistryAdmin head CAS → Runtime 执行前重验
```

## 启动与路由

后端为独立 FastAPI app，默认没有 Investigator、Simulator 或 Agent Tool 路由。前端沿用 React / TypeScript / Ant Design / TanStack Query / React Router，在 `/admin/registry` 下提供 Dashboard、系统列表/详情、Draft 系统和 Capability Editor、变更详情、版本历史与 Diff、Impact Analysis、审批队列、Audit Log 和 Import Preview。

本地启动（两个 PowerShell 终端）：

```powershell
# 仓库根目录：随机生成五个 synthetic 角色的两小时令牌，仅输出在本地终端
.venv\Scripts\python -m scripts.serve_registry_admin --local

# 第二个终端
cd frontend
$env:VITE_REGISTRY_LOCAL='1'
npm run dev
# http://127.0.0.1:5173/admin/registry
```

将对应角色令牌填入本地演示入口；编辑员创建/提交，审批员审核，激活员激活。刷新页面可重新选择另一个 synthetic 角色。令牌保存在页面内存，不写 localStorage、URL、Audit 或数据库。LocalIdentityProvider 存哈希匹配并检查到期时间；必须显式 `local_mode=True`，生产默认不能启用它。`--local` 首次启动只初始化 synthetic Registry，不启动调查循环或金融副作用。

浏览器页面路由 `/admin/registry/*` 与 JSON 请求前缀 `/admin-api/registry/*` 分开。Vite 仅将 `/admin-api` 转发为后端 `/admin`，**不注入调查 Console token**。生产反向代理需要相同映射，SPA 路由回退至 index.html。前端不依赖开发代理提供权限。

## 公司 SSO 与应用 RBAC

`EnterpriseIdentity` 保存 user_id、display_name、groups、authenticated_at，不保存密码。`IdentityProvider` 为可替换接口；生产 `OIDCIdentityProvider` 使用固定 HTTPS issuer/JWKS endpoint 与本 API audience，验证 RS256 签名、issuer、audience、exp、iat、sub，并要求 `registry.admin` scope。它验证 **Access Token**；其他应用的 ID Token／调查 token 不能替代。JWT 算法由服务端固定，不采信 token 提供的算法或远程 key URL。实现参考 [PyJWT 验证接口](https://pyjwt.readthedocs.io/en/stable/api.html)。

SSO 浏览器登录、PKCE 和 refresh 由企业已选定客户端负责，注入 `window.registryIdentity.getAccessToken(): Promise<string>`。应用不自建密码登录。以下为生产组合配置，均来自部署环境，用户不能在 HTTP body/header 覆写：

- REGISTRY_DATABASE_URL、REGISTRY_TENANT_ID：管理实例固定 tenant。
- REGISTRY_OIDC_ISSUER、REGISTRY_OIDC_AUDIENCE、REGISTRY_OIDC_JWKS_URL。
- REGISTRY_GROUP_ROLE_MAPPING：JSON object，`{"company-registry-editors":["REGISTRY_EDITOR"]}` 等。
- REGISTRY_ALLOWED_ORIGINS：确切允许的管理站点 Origin，逗号分隔。

不带 `--local` 启动 `scripts.serve_registry_admin` 使用上述配置；缺少必需项会拒绝启动。真实 SSO 对接必须由部署方配置并验证企业 issuer 的 Access Token contract，本次仅执行本地签名的离线验证。

| Role | Permissions |
|---|---|
| REGISTRY_VIEWER | REGISTRY_VIEW |
| REGISTRY_EDITOR | VIEW / EDIT / SUBMIT / ROLLBACK |
| REGISTRY_APPROVER | VIEW / APPROVE |
| REGISTRY_ACTIVATOR | VIEW / ACTIVATE |
| REGISTRY_AUDITOR | VIEW / AUDIT |

权限实际名称均带 REGISTRY_ 前缀。groups 到 roles 的映射在服务端完成，每个服务入口均检查 permission；前端仅按服务器返回权限隐藏操作。组合多个 group 不取消四眼检查：创建人或提交人即使同时有 APPROVE，也不能 approve/reject 自己的请求。Registry 和 Investigation 的路由、凭据、SSO audience 与权限概念互不混用。

## Change Request 生命周期与事务

`ChangeRequest` 保存请求/tenant、base/proposed Registry version、CR revision、类型、状态、title/reason，以及 created/submitted/approved/rejected/activated 的 actor 和显式 UTC 时间。类型为 CONFIGURATION、ROLLBACK、INVENTORY。

| 动作 | 允许的原状态 | 新状态 |
|---|---|---|
| Submit | DRAFT | SUBMITTED |
| Approve / Reject | SUBMITTED | APPROVED / REJECTED |
| Activate | APPROVED | ACTIVATED |
| Cancel | DRAFT / SUBMITTED | CANCELED |
| Supersede | DRAFT / SUBMITTED / APPROVED | SUPERSEDED |

只有 CONFIGURATION DRAFT 可以编辑。每次编辑通过 RegistryAdmin.register 生成不可变 candidate 版本，Active 不变。提交、审批、拒绝及激活都重验 base；不符返回 STALE_CHANGE_REQUEST。每次变更还要求 expected_revision，状态或 CR revision 不符返回 conflict，终态不能重新编辑或激活。

管理写事务锁 tenant Registry head 后执行 CR 状态 CAS。真正激活仍调用现有 `RegistryAdmin.activate(expected_version=...)`。RegistryAdmin 新增可选 caller-owned Session，因此 Registry publication/head CAS、CR 状态和 Admin Audit 在 **同一事务**提交；任何中途异常全部回滚。原进程内调用方式继续可用，没有通过 HTTP 暴露其原始入口。

两个 Approver 并发只有一方状态 CAS 成功；两个基于同一个 base 的 CR 并发激活只有一方成功，另一方 STALE_CHANGE_REQUEST。模型、Skill、Experience、Reasoning Context 都不获得此管理服务。

## 回滚与不可变历史

回滚只接受本 tenant 曾经 ACTIVATED 的历史版本；未激活的候选不能伪装成历史回滚目标。`POST /admin/registry/rollback` 创建 ROLLBACK DRAFT，继续走 Submit→独立 Approve→Activate，没有直接改 head 的快捷接口。

记录 ROLLBACK_REQUESTED 和 ROLLED_BACK，底层 Registry 仍记录 ACTIVATED。历史 Registry body/hash 不覆盖，Step 14.1 的旧类型兼容投影继续适用。版本列表按登记顺序分页，并标记 Active、曾激活状态和登记时间。

## Typed Diff 与风险

后端 `VersionDiff` 分开返回 systems/capabilities/authority 的 added/removed/changed、routing_scope_changed、status_changed。Changed entry 包含 entity_id、明确 fields 和服务端生成的 before/after values；前端只展示，不自行对 arbitrary JSON 做 diff。credential_ref 的 before/after 也仅显示 `[MASKED]`。

至少下列变化标记 HIGH_RISK：支付权威来源及相关 adapter/system 变化、identity binding 改变、WRITE capability 新增或改变、系统 disable/remove、partner/protocol scope 改变、authority 升为 AUTHORITATIVE。风险标记用于人类审核，不替代硬模型校验；所有级别仍需独立审批。PAYMENT_FINALITY 的 subject/identity/current/complete 强约束与 READ Tool contract 的 claims 上限继续由 RegistryDefinition/RegistryAdmin 校验，管理 UI 无权绕过。

## Impact Analysis

Impact 是纯读；返回 affected_active_case_count、Case IDs digest、partner/product 范围、capability types、invalidated_snapshot_count、pending work count，以及分页 Case drill-down。不会把所有 Case ID 放入摘要，不修改 Case、Snapshot、Work 或路由。

第一版明确采用 `CONSERVATIVE_REGISTRY_VERSION_FENCE`：现有 Runtime 用全局 Registry version 做保守重验，因此一次配置版本变化会影响当前 tenant 所有已绑定路由的非终态 Case，即使部分能力资格未变。快照统计仅计 base version 的相关历史快照；待办包含 PENDING/READY/CLAIMED/BLOCKED。Impact 是分析时刻的估计，不是冻结执行承诺；页面明确显示 base 是否仍有效。真正激活后，既有 Runtime 自然返回 STALE_CAPABILITY 并重建 Context。

纯库存资料更新不改变 Registry definition/hash，故不导致业务能力快照失效。没有注册 Route 的 legacy Case 不计入本版 Registry fence。

## Excel：公司资料与能力配置两层

`CompanySystemInventory` 忠实保存 system_code、system_name、system_category、description、main_functions。source_metadata 保存 EXCEL、原文件名/hash、sheet!row、imported_at/imported_by。原始工作簿不改写、不进入模型上下文。

支持 `.xlsx` 上传，包括需求中的中文文件名。明确表头别名如下（第一行必须是表头，不猜测自由文本）：

| 字段 | 支持的常见表头 |
|---|---|
| system_code | system_code / 系统编码 / 系统英文名 / 系统英文名称 / 英文名称 |
| system_name | system_name / 系统名称 / 系统中文名 / 系统中文名称 / 中文名称 |
| system_category | system_category / 系统分类 / 系统类别 |
| description | description / 系统简介 / 简介 / 系统描述 |
| main_functions | main_functions / 主要功能 / 主要功能说明 |

code/name 必需，其余可以为空。实际公司文件没有提供，因此本次使用上述文件名的 synthetic 工作簿做测试；未宣称已经解析或导入公司的真实资料。若真实资料列名不同，需要显式扩充映射，不用模型猜字段。

上传→受限解析→added/changed/unchanged/invalid preview→用户确认→INVENTORY Draft→审批→激活库存版本。Preview 不修改 Registry 或 active inventory；确认也只创建 Draft。Inventory CR 使用独立 inventory base revision，并校验 Registry base，避免同时导入覆盖。重复确认同一个 preview 是幂等的。审批详情展示待导入资料及来源；旧库存 snapshot 永久保留，导入是按 system_code 合并，不删除工作簿未包含的资料。

Excel 的 authority、Tool、WRITE 等额外列不会被映射。AgentCapabilityConfiguration 仍由独立 RegistryDefinition 管理，只有显式人工配置和审批才能更新。系统名称“支付系统”不证明任何 Claim 权威性，导入不能自动绑定 PAYMENT_FINALITY。

上传限制为 XLSX 5 MB、解压总量 25 MB、300 个 ZIP entries、5000 数据行、100 列；拒绝宏、公式、损坏 ZIP、重复编码和无效行。openpyxl 以只读模式解析，关闭 external link 保留，使用 defusedxml；读取/解析异常统一返回安全错误。存在 invalid 行就不能确认，不静默丢弃问题资料。HTTP 请求体另限制 6 MB。

## Secret / CSRF 边界

API 只接受显式 Authorization Bearer，完全不使用 cookie 认证；因此不设置应用 session cookie，也不依赖浏览器自动附带身份。前端使用 `credentials: omit`。生产链路应由可信反向代理提供 HTTPS。

POST/PATCH 额外要求 `X-Registry-Request: 1`，带 Origin 时必须精确命中部署 allowlist；不开放 CORS。这会拒绝跨站 form/simple requests。没有 header/role/tenant impersonation 入口。API 响应 `Cache-Control: no-store`，请求校验失败不会回显 Pydantic rejected input，避免意外 echo 提交的敏感值。

System credential_ref 仅服务器保存，GET 默认 `[MASKED]`；PATCH 只接受 marker/None，服务器从原 candidate 保存的 System ID 恢复真实引用，不允许浏览器替换、删掉或读取它。新系统的 credential/adapter 绑定由可信部署流程维护。没有 API key、password、raw credential 编辑框或返回字段。普通业务文本不是秘密存储位置，本项目没有实现通用 DLP 产品。

## 审计与存储

新增 registry_change_requests、registry_admin_audit、registry_inventory_heads、registry_inventory_versions、registry_inventory_previews。原 Registry 版本、head、来源与 Case revision 表继续使用。

Audit 记录 CREATE_DRAFT、EDIT_DRAFT、IMPORT_PREVIEW、IMPORT_CONFIRMED、SUBMITTED、APPROVED、REJECTED、ACTIVATED、ROLLBACK_REQUESTED、ROLLED_BACK、CANCELED、SUPERSEDED，以及 actor、time、request_id、before/after version、CR ID。Admin Audit 没有 PATCH/DELETE API；应用仅 append，历史版本也不修改。平台 DBA 的数据库权限治理属于部署职责，不把应用 append-only 冒充不可篡改 WORM 存储。没有在日志/Audit 保存 token、真实 credential 或模型私有推理。

## API 概览

全部前缀 `/admin/registry`，且全部要求认证与对应应用权限：

- GET `/`、`/catalog`、`/systems`、`/systems/{id}`、`/inventory`。
- GET `/versions`、`/versions/{version}`、`/versions/{a}/diff/{b}`。
- GET/POST `/change-requests`，GET/PATCH `/change-requests/{id}`。
- POST `/change-requests/{id}/{submit|approve|reject|activate|cancel|supersede}`。
- POST `/rollback` 创建回滚 Draft。
- GET `/change-requests/{id}/impact`，分页 drill-down。
- POST `/import/preview`（multipart），POST `/import/confirm`。
- GET `/audit`，仅 AUDIT 权限。

集合与 drill-down 参数 page>=1、1<=size<=100；没有向浏览器暴露管理 app OpenAPI/Swagger 入口。Investigation 的 API 未挂载在这个 app。

## 验证与范围

后端测试见 `tests/test_registry_admin.py`；包含真实离线 RSA JWT 签名与 claims 验证、角色/四眼边界、跨租户、并发状态/激活 CAS、激活与 Audit 原子回滚、只读 Impact、Excel 不产生能力/权威/WRITE、凭据脱敏、历史回滚资格与 Runtime 旧快照拒绝。前端测试覆盖系统列表/详情、Capability Editor、Diff、审批队列、Impact、Import Preview、权限隐藏、403 和 stale conflict；还继续运行原调查 Console 测试。

```powershell
.venv\Scripts\python -m pytest tests/test_registry_admin.py -q
# PostgreSQL 使用专门测试数据库，每个测试独立 schema
$env:TEST_POSTGRES_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/TEST_DATABASE'
.venv\Scripts\python -m pytest tests/test_registry_admin.py --postgres -q
# 可选本地并行测试依赖，不是 Multi-Agent 功能
.venv\Scripts\python -m pip install -e '.[test-parallel]'
.venv\Scripts\python -m pytest -n 2
.venv\Scripts\python -m pytest -n 2 --postgres
cd frontend
npm test
npm run build
```

完整离线治理 Demo：`python -m scripts.demo_registry_admin`。它在新的本地 SQLite 文件中演示 Draft→Submit→拒绝自审批→独立审批→激活→旧快照失效→经审批回滚，并打印实际 CR、typed diff、Impact 和 Audit。示例结果为 `SELF_APPROVAL_REJECTED`、`STALE_CAPABILITY`、`restored_original=true`、`financial_effects_executed=false`。

浏览器实际验证了编辑员登录、12 项系统清单、资金接入语义、Draft 创建、字段编辑生成新 candidate，以及 Submit 后没有审批/激活动作。演示记录在本地独立 registry-admin.db 中，未修改任何真实金融系统。

没有实现自建密码、完整企业 IAM、真实公司 SSO 凭据、Vault/KMS、动态插件、Vector/Embedding、Multi-Agent、LangGraph 或 Money Movement。OIDC/反向代理配置与公司 SSO 联调仍是部署工作；本地和离线测试不等于完成公司生产上线。

## 本次实际验收记录

新增 42 项管理后台测试；Step 14.1 的 1299 项既有后端测试保留，总计 1341 项。修复了独立后台测试暴露的 Recovery→Ledger 表元数据导入顺序依赖，仅增加声明式表依赖，不改变副作用行为。

| 验证 | 结果 |
|---|---|
| SQLite 独立 Admin backend | 42 passed，47.25s |
| PostgreSQL 独立 Admin backend | 42 passed，56.66s |
| SQLite full suite（2 个隔离 pytest worker） | 1338 passed，3 skipped；1149.56s |
| PostgreSQL full suite（2 个隔离 pytest worker） | 1339 passed，2 skipped；1307.66s |
| 前端（含原 Investigation Console） | 31 passed；独立串行复跑 27.74s |
| TypeScript + Vite production build | 通过；存在 bundle 大于 500 kB 的体积提示 |

实际日志位于 `.local/step142-standalone-final.log`、`.local/step142-standalone-pg-final.log`、`.local/step142-final-full-sqlite.log`、`.local/step142-final-full-postgres.log`、`.local/step142-ui-final-verified.log`、`.local/step142-ui-build-final.log`。SQLite 跳过 1 项 PostgreSQL 专用测试和 2 项可选在线 LLM 测试；PostgreSQL 只跳过这 2 项在线 LLM 测试。两组后端全量各有 3 条既有 Starlette/AnyIO 弃用提示。

前端在后端并行测试期间两次出现超时；测试数据库完成检查点并关闭后，以 `npm test -- --maxWorkers=1 --no-file-parallelism` 独立复跑全部通过，没有放宽断言或超时。离线治理 Demo 的实际 CR/Impact/Diff/Audit 输出为 `.local/step142-demo.json`。本次浏览器验收只创建和提交 synthetic Draft，没有进行真实金融操作。
