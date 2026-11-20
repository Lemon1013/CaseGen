# CaseGen 产品梳理与重构决策记录

- **创建日期**：2026-11-20
- **当前阶段**：全量产品决策已定稿，进入架构设计与分阶段重构阶段
- **文档用途**：持续记录 CaseGen 重构前的产品取舍、模块边界、零破坏契约及后续架构重构约束，作为设计方案与任务拆分的权威输入。

---

## 一、模块决策总览

| 编号 | 业务模块 | 决策状态 | 结论摘要 |
| :--- | :--- | :--- | :--- |
| 1 | 多数据池 & 平台适配 / 平台用例生成 | **已定稿** | 保留并冻结（他人提交的功能，与主流程解耦，本轮重构不动，但须遵守兼容保护契约） |
| 2 | 核心用例生成：传统任务流水线 vs AI Agent 对话（DSH） | **已定稿** | 流水线为主干（保留双检查点/评审/需求优化/单任务 Prompt 优化，禁全局改写，清理旧整篇分支统一逐点生成，强化健壮性）；Agent 为探索入口并预留流水线阶段接入点（如多轮检索）；Agent 产物改为按消息 ID 弹窗归属（绑已有/新建预填需求）直入用例库；保留专家技能 |
| 3 | Wiki 2.0 知识库（含文档摄入、审阅、检索、共享/外部库） | **已定稿** | 彻底下线内嵌 Wiki 2.0 知识库及 4 个前端页面；统一以外部知识库为唯一知识源，支持多异构外部知识库 Provider 插件化扩展（首期内置 `llm_wiki_django` 对接 Django Session Cookie 鉴权与 `/files/`、`/file-content/` 全文读取）；未配置或未命中时明确提示且允许用户确认后继续；底层静默保留默认 `WikiSpace` 壳保护模块 1 零破坏 |
| 4 | 项目 / 空间 / 系统管理（模型、提示词、用户与权限） | **已定稿** | 统一更名「项目管理」并精简侧边栏为 3 组；提示词按类型分级治理（引擎类归 `admin`，`generate` 支持 `system`/`shared`/`private` 且严禁 `admin` 查看用户 `private` 模板，支持 Fork 复制）；收口底座缺陷；权限维持 `admin`/`user` + `is_public`，不引入 RBAC |
| 5 | 数据看板（Dashboard）与可观测性 | **已定稿** | 精简保留（剔除已作废的文档/Wiki 指标），优化前后端实现方式（拆组件、复用统一状态字典、修复 8 小时时区 Bug、优化聚合查询） |
| 6 | 数据兼容策略 & 外围工具链（`tools/`、`.qa/`、`fixtures/`） | **已定稿** | 保留并平滑兼容现有 `app.db` 老数据（引入 `schema_migrations` 版本表管理增量迁移）；`tools/` 下脚本保持不动；升级 `mock_external_wiki` 对齐 `API_README.md`；清理仓库临时残留文件与无用死代码 |

---

## 二、逐模块决策详情

### 模块 1：多数据池 & 平台适配 / 平台用例生成

- **决策状态**：已定稿（2026-11-20）
- **决策结论**：**完整保留、冻结不动（不纳入本轮重构修改范围）**。
- **决策依据**：
  - 属于业务所需能力，且为其他协作者提交的独立模块；
  - 经代码核实，其数据与逻辑已与核心生成流水线（`task_pipeline.py`）基本解耦。
- **冻结范围（只读保护，不得破坏）**：
  - 后端：`backend/app/api/platform_data.py`、`backend/app/schemas/platform_data.py`、11 张平台/数据池相关表（`DataPool`、`DataPoolRevision`、`PlatformProfile`、`ExampleVariant`、`PlatformExample`、`PlatformRenderRun`、`PlatformArtifact`、`ManagedPlatformCase` 等）；
  - 前端：`DataPoolsView.vue`、`PlatformManagementView.vue`、`PlatformCaseGeneratorView.vue`、`PlatformCasesView.vue`、`PlatformUploadView.vue`、`ExampleContentPreview.vue`、`api/platformData.ts` 及对应路由/菜单入口。
- **本轮重构必须遵守的兼容保护契约（防误伤清单）**：
  1. 保留 `Project` → `defaultWikiSpaceId`（`WikiSpace`）映射语义，确保平台视图与接口可继续按默认空间隔离读写；
  2. 保留 `TestCase` 表结构及可读语义用例接口，确保平台生成器可继续读取用例库语义用例；
  3. 保留 `ModelConfig` 与内置 Prompt 类型 `data_description_parse`、`platform_example_render`；
  4. 重构 `frontend/src/api/client.ts`（`project_id` 注入/错误处理）与 `MainLayout.vue`（导航分组/路由）时，须回归验证这 5 个页面及 `/api/data-pools`、`/api/platforms`、`/api/platform-renders`、`/api/platform-cases` 路由可用。
- **已知问题备忘（本轮暂不处理，留作后续专项或交原维护者）**：
  - `PlatformUploadView.vue` 当前仅为历史产物下载页，尚未接通真实平台推送连接器；
  - `render_example_direct` 与 `create_data_pool(parse_with_llm=True)` 在 HTTP 线程同步阻塞调用 LLM；
  - 部分读写接口未强制校验 `project_id`，且 `PlatformArtifact` 与 `ManagedPlatformCase` 存在列表查询回填开销。

### 模块 2：核心用例生成（传统任务流水线 vs AI Agent 对话）

- **决策状态**：已定稿（2026-11-20）
- **战略定位与演进方向**：
  - 传统任务流水线是平台核心主干，整体流程保持不变；
  - AI Agent 对话（对接 DSH）定位为探索性验证入口（已验证 Agent 能力接入可行）；
  - **中长期演进方向**：将 Agent 能力按阶段逐步融入流水线关键环节（例如将知识库检索交由 Agent 自主执行多轮检索），因此保留 `agent_gateway.py` 基础能力并在流水线重构时为关键阶段预留清晰的策略/扩展接口（不再堆砌无调用方的散落配置）。
- **传统流水线保留与裁剪边界**：
  - **保留**：双人工检查点（检索确认 + 测试点确认）、AI 评审（可选步骤）、需求优化（`requirement_optimize`）、Prompt 自动优化（可选步骤，仅限单任务临时生效）；
  - **下线/禁止**：
    - 禁止在任务优化流中直接修改全局生效 Prompt（下线 `apply_prompt(mode="global")`），防止多人/多项目互相污染；
    - 删除 `task_pipeline.py` 中基于 `PYTEST_CURRENT_TEST` 的旧整篇生成分支（含对应仅测整篇路径的旧测试），统一生产与测试均走逐点生成，将精简上下文降级思路收口为单点异常降级重试；
  - **重点目标**：把**流水线健壮性**作为本轮重构核心优先级（拆分 `task_pipeline.py` / `TaskDetailView.vue` 巨型文件、补齐 `retrieving`/`reviewing`/`optimizing` 重启恢复、后台任务并发限流、修复 `### 待确认项` 解析 Bug、统一前后端 SSE 与结构化用例卡片组件）。
- **AI Agent 对话收口方案**：
  - **产物出口**：点击具体 assistant 气泡的「入库至用例管理」弹出入库归属弹窗，支持二选一：① 绑定当前项目下已有需求（遇同 `case_key` 冲突走冲突策略）；② 新建需求（自动预填会话标题 + 用户提问与附件文件名摘要，支持人工编辑）后原子入库 `TestCase`，不再创建壳 `GenerationTask`；同时修复历史气泡按钮错位 Bug，支持按具体消息 ID 精准转存；
  - **专家技能（`.agents/skills/`）**：保留并作为后续新增定制化业务技能的标准扩展入口。

### 模块 3：Wiki 2.0 知识库与外部知识库

- **决策状态**：已定稿（2026-11-20）
- **核心结论**：
  - **彻底下线内嵌 Wiki 知识库**：项目内部不再承担文档导入、切块、Wiki 编译、风险审阅、版本回滚、共享知识库与本地 FTS5 检索等职责；
  - **统一以外部知识库（`ProjectExternalWiki`）为唯一知识源**：并将其客户端与检索服务作为核心基础能力重点加固（超时配置、重试、熔断、显式错误处理、只读检索抽象以供未来 Agent 多轮检索复用）。
- **计划下线/清理范围**：
  - 后端：`api/documents.py`、`api/wiki.py`（1,901 行含 675 行 purge）、`wiki_*.py`（16 个 Wiki 编译/仓储/FTS/审阅/迁移等服务）、`source_chunking.py`、`source_chunks_store.py`、`clause_index.py`、`document_delete.py`、`retrieve.py`、`default_wiki/`、`wiki_analyze.md`/`wiki_write.md` 提示词、本地知识冲突裁决（`TaskKnowledgeDecision`/`ProjectKnowledgeDecision`）及 FTS5 虚表；
  - 前端：`DocumentsView.vue`、`WikiView.vue`、`WikiReviewView.vue`、`WikiSpacesView.vue`、`api/documents.ts`、`api/wiki.ts` 及侧边栏整个「Wiki 知识库」分组，同时移除工作台与任务创建页的 Wiki 空间选择器。
- **必须精准保留的关联组件（防误伤）**：
  1. **`parse_document.py` 保留**：供需求文档导入（`POST /api/requirements/import-doc`）和 Agent 聊天附件解析（`agent_attachments.py`）继续使用；
  2. **底层静默壳 `WikiSpace` 保留（履行模块 1 零破坏契约）**：因模块 1 的 `DataPool`/`PlatformProfile`/`DataPoolsView`/`Platform*View` 仍依赖 `wiki_space_id` / `projects.defaultWikiSpaceId` 及只读 `listWikiSpaces()`，底层保留 `WikiSpace` 表与每项目自动关联的默认私有 `default_wiki_space_id`（前端隐藏“Wiki 空间”管理概念），确保模块 1 完全不动且零回归；
  3. **清理 Agent 本地 Wiki 路径注入**：移除 `agent_turn.py` / `agent_attachments.py` 对本地 `data/wiki/` 的路径引用，预留外部知识库查询工具接入位。
- **外部知识库交互与接口契约**：
  - **空知识/未命中兜底策略**：
    - 项目未配置外部知识库：向用户明确提示「该项目未配置知识库，可联系管理员添加」，用户确认后允许继续后续测试点与用例生成；
    - 已配置但检索未命中：提示「未检索到相关内容」，用户确认（可补充背景）后允许继续后续步骤；
  - **鉴权与接口契约（对齐 `docs/llm-wiki-reference/API_README.md`）**：
    - 鉴权方式：Django Session Cookie；在 `ProjectExternalWiki` 配置中支持填写登录账号与密码（编辑留空不改、接口出参严格脱敏不回显），由 CaseGen 后端 `external_wiki_client.py` 通过 `httpx.Client` CookieJar 自动调用 `POST /api/auth/login/` 建立会话并在 401/403 时自动重登续期（前端全部经 CaseGen 后端代理，不直连外部服务、避免跨域 Cookie 与凭据泄露）；
    - 核心对接端点：`POST /api/auth/login/`、`GET /api/auth/me/`、`GET /api/projects/`、`POST /api/projects/{project_id}/search/`、`GET /api/projects/{project_id}/files/`、`GET /api/projects/{project_id}/file-content/?path=...`；
    - 客户端加固与演进：支持可配置超时、有限重试、熔断降级与明确错误透传；通过后端代理向 `CitationList` 抽屉提供外部文件正文预览，并为未来 Agent 多轮自主检索（`search` → `files` → `file-content`）提供统一只读工具能力。
- **多异构外部知识库扩展入口设计（已定稿）**：
  - **业务目标**：后续可能同时对接多个接口协议不同的外部知识库，需在模型、后端和前端留出标准化扩展入口，做到「新增一种外部知识库 = 新增一个 Adapter，主流水线与前端零侵入改动」；
  - **数据模型演进**：由单项目 1:1 外部库演进为 **1:N 多知识源配置**（支持 `provider_type` 枚举如首期 `llm_wiki_django`、`auth_type` 如 `django_session`/`bearer`/`api_key`/`none`、`config_json` 适配器专属参数、`credentials_json` 敏感凭据独立脱敏存储、`weight`/`priority`/`enabled`）；
  - **后端适配器抽象（`ExternalKnowledgeProvider` Protocol + `PROVIDER_REGISTRY`）**：
    - 统一最小接口：`test_connection`、`list_remote_scopes`（可选能力）、`search -> list[KnowledgeHit]`、`list_files` / `read_file_content`（可选能力，按 capability flag 声明）；
    - 首期内置实现 `LlmWikiDjangoProvider`（对接上述 Django Session Cookie + `/projects/` + `/search/` + `/files/` + `/file-content/`）；
    - 统一检索服务按项目聚合所有已启用的外部知识源并归一排序，未来 Agent 多轮检索也直接调用该统一抽象层；
  - **前端动态配置入口**：项目管理中外部知识库从单表单升级为**多知识源列表**，新建/编辑时通过 `GET /api/knowledge-providers/types` 动态拉取已注册适配器类型与能力描述，按 `provider_type` 渲染对应鉴权与参数表单。

### 模块 4：项目 / 空间 / 系统管理（模型、提示词、用户与权限）

- **决策状态**：已定稿（2026-11-20）
- **4.1 概念与导航收口（已定稿）**：
  - 原「空间管理（`/spaces`）」更名为「项目管理」，承载项目配置与多异构外部知识库列表；
  - 「数据池」菜单归类移入「平台适配」分组（路由与代码保持模块 1 冻结不变）；
  - 侧边栏整体从 4 组 18 项精简为 3 组清晰入口（用例中心 / 平台适配 / 系统配置）。
- **4.2 提示词分级权限管理（已定稿）**：
  - 清理废弃的 `wiki_analyze`、`wiki_write` 类型；
  - **系统引擎类**（`review`、`test_points`、`requirement_optimize`、`optimize` 及模块 1 冻结的 `data_description_parse`、`platform_example_render`）：仅 `admin` 可见与维护，普通用户不可见、不可选、不可改；
  - **用例生成类（`generate`）**：`PromptTemplate` 新增 `owner_user_id`（`NULL` 为系统预置）与 `visibility`（`system` / `shared` / `private`）；
    - 存量及管理员预置 `system` 模板：全员可见可用、普通用户不可改删；
    - 个人 `private` 模板：严格仅作者本人可见可用可改删，**管理员不可查看普通用户的私有模板**；
    - 团队 `shared` 模板：全员可见可用，仅作者本人及管理员可治理（编辑/删除/撤回）；
    - 前端支持基于 `system`/`shared` 模板一键「复制为我的模板（Fork）」，工作台下拉按「系统预置 / 我的模板 / 团队共享」分组展示；后端创建任务严格校验模板权限与存在性，撤回/删除不影响历史草稿（仅禁止删除正被进行中任务引用的模板）。
- **4.3 通用底座与顶栏健壮性修复（已定稿）**：
  - 修复 `api/client.ts` 全局接口（`/api/projects`、`/api/users`、`/api/models` 等）不再盲目注入 `project_id`，收口 `api()` 与 `apiBlob()` 重复逻辑；
  - 修复 `MainLayout.vue` 切项目导致 `skipProjectGate` 全局页误销毁重建问题，取消每次切路由盲目拉取 `listModels()`，在 `/agent` 页面隐藏无关的流水线默认模型徽章；
  - 优化后端鉴权中间件，消除单请求多次开启 SQLite Session 与散落正则重复校验。
- **4.4 用户管理与权限模型（已定稿）**：
  - 不引入 RBAC，维持现有 `admin` / `user` 两级角色 + 项目 `is_public` 开关 + 自助注册开关逻辑不变。

### 模块 5：数据看板（Dashboard）与可观测性

- **决策状态**：已定稿（2026-11-20）
- **决策结论**：**精简保留并重构优化**。
- **重构范围与优化方向**：
  - 移除已作废的文档/Wiki 统计指标，聚焦**用户 / 项目 / 任务 / 用例 / 评审质量 / 平台渲染 / Agent 对话**核心指标；
  - 重构 `DashboardView.vue`：拆分子组件、优化图表实现、修复 8 小时时区偏移 Bug、复用统一状态与优先级字典；
  - 精简后端 `dashboard.py`，优化聚合查询，清理无用统计逻辑。

### 模块 6：历史数据兼容 & 外围工具链与仓库卫生

- **决策状态**：已定稿（2026-11-20）
- **老数据平滑保留**：
  - 兼容现有 `data/meta/app.db` 全部业务数据（用户 / 项目 / 模型 / 提示词 / 任务 / 用例 / 平台数据 / Agent 会话）；
  - 引入 `schema_migrations` 版本表替代无版本全量探表；
  - 旧 Wiki 历史表温和留存不暴力 `DROP`，代码层停止读写。
- **`tools/` 保持不动**：
  - `tools/runtime_validation/`、`tools/scenario_benchmark/` 不纳入本轮修改范围。
- **Mock 外部知识库升级**：
  - 保留并升级 `backend/mock_external_wiki/server.py`，对齐 `docs/llm-wiki-reference/API_README.md`（补齐 Django Session 登录 `/api/auth/login/`、`/api/auth/me/` 以及 `/files/`、`/file-content/` 接口）。
- **临时文件与死代码清理**：
  - 清理根目录及前后端残留临时文件（`casegen.zip`、`压缩文件.zip`、`backend/nohup.out`、`frontend/nohup.out`、`test.py`、`开箱即用-前端.txt` 等，严禁误删有效源码与 `data/`）；
  - 清理零引用死代码（如 `HelloWorld.vue`）。

---

## 三、待决事项追踪 & 下一步实施计划

- [x] 模块 1：多数据池 & 平台适配 / 平台用例生成（已定稿并冻结）
- [x] 模块 2：传统流水线 vs AI Agent 对话（已定稿）
- [x] 模块 3：Wiki 2.0 知识库下线与外部知识库对接（已定稿）
- [x] 模块 4：项目 / 空间 / 系统管理（模型、提示词分级权限、底座与用户权限）（已定稿）
- [x] 模块 5：Dashboard 看板精简与前后端优化（已定稿）
- [x] 模块 6：历史 SQLite 数据兼容、`tools/` 冻结、Mock 升级与仓库卫生（已定稿）

> **下一步实施计划**：详见 [2026-11-20-casegen-refactor-plan.md](../plans/2026-11-20-casegen-refactor-plan.md)（《CaseGen 架构重构方案与分阶段实施计划》）。
