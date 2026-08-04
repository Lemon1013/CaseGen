# CaseGen Wiki 2.0 实施计划

**目标：** 将当前按文档生成摘要页的 Wiki，升级为具有增量更新、来源追踪、审核、版本和维护能力的金融测试知识库。  
**规格：** `docs/superpowers/specs/2026-08-02-wiki-core-redesign-design.md`  
**原则：** 分阶段保持现有上传、Wiki 浏览和用例生成可用；先建立核心模型与安全写入，再升级检索。

## 交付顺序

```text
基础模型与迁移
  → purpose/schema 与页面仓储
  → 真正异步的摄入 Job
  → 增量变更计划
  → 原子应用与版本
  → Review / Diff / Rollback
  → index / overview / log / lint
  → FTS 检索与评测
  → 前端工作流
```

## Task 1：锁定 Wiki 配置与页面契约

**新增：**

- `data/wiki/purpose.md` 默认模板
- `data/wiki/schema.md` 默认模板
- `backend/app/services/wiki_schema.py`
- `backend/tests/test_wiki_schema.py`

**工作：**

- [x] 定义允许的页面类型：`source`、`rule`、`entity`、`scenario`、`regression`、`synthesis`。
- [x] 定义并校验 `page_key`：小写、点号分段、不可由模型提供文件路径。
- [x] 实现 frontmatter 解析、规范化和序列化。
- [x] 启动时仅在文件不存在时写入默认 `purpose.md`、`schema.md`。
- [x] 测试非法类型、重复 key、缺少来源和路径穿越输入。

**完成标准：** 同一页面对象序列化再解析后语义不变；关键规则页缺少来源时校验失败。

## Task 2：扩展数据库并迁移旧页面

**修改：**

- `backend/app/models/entities.py`
- `backend/app/db.py`
- `backend/app/services/wiki_migrate.py`
- `backend/tests/test_wiki_migrate.py`

**工作：**

- [x] 为 `wiki_pages` 增加 `page_key/domain/status/revision/aliases_json/content_hash`。
- [x] 新建 `WikiPageSource`、`WikiPageRevision`、`WikiReviewItem`。
- [x] 扩展 `IngestJob` 的阶段、进度、计划、版本和取消字段。
- [x] 将现有页面迁移为稳定 legacy key；保留文件和原 ID。
- [x] 为 `page_key`、Job 状态和来源关系增加索引/唯一约束。
- [x] 迁移可重复执行，并先备份 SQLite 数据库。

**完成标准：** 旧数据升级后仍能通过现有 `/api/wiki/pages` 浏览和检索。

## Task 3：建立页面仓储与原子写入

**新增：**

- `backend/app/services/wiki_repository.py`
- `backend/app/services/wiki_staging.py`
- `backend/tests/test_wiki_repository.py`

**工作：**

- [x] 统一页面路径解析，禁止 API/LLM 直接拼接磁盘路径。
- [x] 实现按 `page_key` 读取、创建、更新、归档和列出页面。
- [x] 每次变更写入 revision，并计算内容 hash。
- [x] 在临时 staging 目录写候选文件并校验后再替换正式文件。
- [x] 失败时回滚数据库并清理 staging，旧页面与索引保持不变。
- [x] 增加并发锁，禁止同一文档或 page_key 同时写入。

**完成标准：** 注入任意写文件异常后，正式 Wiki 与数据库均保持旧版本。

## Task 4：实现持久化摄入 Job

**修改/新增：**

- `backend/app/api/documents.py`
- `backend/app/api/wiki.py`
- `backend/app/services/wiki_jobs.py`
- `backend/tests/test_wiki_jobs.py`

**工作：**

- [x] 摄入接口创建 `queued` Job 并在 500ms 内返回。
- [x] 后台 worker 串行领取 Job；启动时恢复 `queued/running` Job。
- [x] 实现阶段、百分比、结构化步骤、取消和失败重试。
- [x] 同文档已有活动 Job 时返回现有 Job 或 409。
- [x] 使用 SHA256 + schema/Prompt 版本跳过未变化来源，支持 `force=true`。
- [x] 避免 LLM HTTP 重试与窗口重试乘法放大，统一重试策略。

**完成标准：** 浏览器刷新或服务重启后仍可继续查看 Job；前端无需维持原始 POST 请求。

## Task 5：提升解析与来源锚点

**修改：**

- `backend/app/services/parse_document.py`
- `backend/app/services/source_chunking.py`
- `backend/app/models/entities.py`
- `backend/tests/test_parse_document.py`
- `backend/tests/test_source_chunks.py`

**工作：**

- [x] 解析结果改为 `ParsedDocument`，包含正文、页码/章节范围和质量诊断。
- [x] 空文本、疑似扫描 PDF、乱码率过高时阻止摄入并给出可读错误。
- [x] SourceChunk 保存页码、章节、条款号和父级段落。
- [x] 修正前端 `.doc` 与后端支持范围不一致。
- [x] 保留现有字符范围用于兼容旧引用。

**完成标准：** 每条关键规则可定位至至少一个 SourceChunk；低质量解析不会进入 LLM。

## Task 6：相关页面召回与 Step A 变更计划

**新增/修改：**

- `backend/app/services/wiki_candidates.py`
- `backend/app/services/wiki_plan.py`
- `backend/app/services/wiki_long_analyze.py`
- `backend/app/default_prompts/wiki_analyze.md`
- `backend/tests/test_wiki_plan.py`

**工作：**

- [x] 分析前按标题、别名、实体、条款和标签召回现有页面。
- [x] 将 `purpose/schema`、候选页摘要和来源窗口传入分析模型。
- [x] Step A 输出 `claims/related_pages/contradictions/page_operations/review_items`。
- [x] 使用 Pydantic 严格校验 JSON，拒绝未知操作、非法 key 和无效锚点。
- [x] 每窗结果持久化；滚动摘要采用重新压缩或头尾配额，不能永久丢弃后窗。
- [x] 按条款、实体和语义 key 合并，避免“最先 80 条”偏置。

**完成标准：** 第二份同主题来源产生 `update/noop`，而不是另建重复规则页；尾部规则进入计划。

## Task 7：Step B 候选页面与变更应用

**新增/修改：**

- `backend/app/services/wiki_apply.py`
- `backend/app/services/wiki_pages_parse.py`
- `backend/app/default_prompts/wiki_write.md`
- `backend/tests/test_wiki_apply.py`

**工作：**

- [x] 实现 `create/update/noop`，随后实现受审核保护的 `merge`。
- [x] 写页模型只能返回结构化页面内容，路径由仓储层决定。
- [x] 校验页面正文、wikilink、来源、page_key 和页面数量。
- [x] 更新时保留旧来源、别名和未被明确否定的有效规则。
- [x] 数值变化、删除规则、冲突和合并生成 ReviewItem。
- [x] 来源摘要页始终存在；LLM 失败时使用确定性 fallback。

**完成标准：** 所有自动应用变更都有 revision、Job、理由和来源；高风险变更不会静默发布。

## Task 8：Index、Overview、Log 与 Lint

**新增/修改：**

- `backend/app/services/wiki_index.py`
- `backend/app/services/wiki_overview.py`
- `backend/app/services/wiki_log.py`
- `backend/app/services/wiki_lint.py`
- `backend/tests/test_wiki_lint.py`

**工作：**

- [x] Index 按 domain/type 分类并包含摘要、状态和来源数。
- [x] Overview 汇总领域、主要规则、冲突和知识缺口。
- [x] Log 追加摄入、审核、回滚和 Lint 记录。
- [x] Lint 检查重复 key、死链、孤立页、无来源规则、冲突、缺失文件和索引漂移。
- [x] Lint 修复必须走候选 Diff，不直接修改正式页面。

**完成标准：** 删除或回滚后无死链；Index 可作为中等规模 Wiki 的有效导航入口。

## Task 9：Review、Diff 和回滚 API

**修改：**

- `backend/app/api/wiki.py`
- `backend/app/schemas/wiki.py`
- `backend/tests/test_wiki_review_api.py`

**工作：**

- [x] 列出/筛选 pending ReviewItem。
- [x] 展示旧版、新版、结构化理由和来源证据。
- [x] Approve 原子应用候选修订；Reject 保留审计记录。
- [x] 列出页面 revisions，支持回滚并生成新 revision。
- [x] 审核接口验证状态机，防止重复批准。

**完成标准：** 冲突规则必须经批准才进入 published 页面，且可完整回滚。

## Task 10：全文检索与上下文组装

**新增/修改：**

- `backend/app/services/wiki_fts.py`
- `backend/app/services/hybrid_retrieve.py`
- `backend/app/services/task_pipeline.py`
- `backend/tests/test_wiki_retrieval_eval.py`

**工作：**

- [x] 为 Wiki 和 SourceChunk 建立 SQLite FTS5 索引与增量更新。
- [x] 标题、别名、标签、条款号加权，正文使用 BM25。
- [x] 从高分页通过 wikilink/共享来源扩展一跳，并抑制重复结果。
- [x] snippet 围绕命中位置；上下文按页公平分配预算。
- [x] 仅对查询中的明确条款号强锚定原文。
- [x] 保留现有检索响应字段，新增 explain 信息采用可选字段。
- [x] 建立至少 30 条真实查询的评测 fixture。

**完成标准：** Recall@5 ≥ 85%，首条结果可解释且摘要包含命中词，生成引用可回到原文。

## Task 11：前端摄入与 Wiki 工作流

**修改/新增：**

- `frontend/src/views/DocumentsView.vue`
- `frontend/src/views/WikiView.vue`
- `frontend/src/views/WikiReviewView.vue`
- `frontend/src/api/documents.ts`
- `frontend/src/api/wiki.ts`

**工作：**

- [x] 上传后展示解析质量和原文预览。
- [x] 恢复并轮询活动 Job，显示阶段、窗口进度、重试、取消。
- [x] Wiki 按 domain/type 分组，支持来源、状态和标签筛选。
- [x] 展示命中高亮、来源锚点、关联页面和完整原文跳转。
- [x] 增加 Review Diff、批准、拒绝、revision 和回滚界面。
- [x] 错误信息转换为可操作的中文提示。

**完成标准：** 用户可从上传一直跟踪到发布，并能理解每次 Wiki 修改了什么、依据是什么。

## Task 12：迁移、回归与发布

- [x] 备份真实 `data/`，在副本上运行迁移和重建。
- [x] 运行全部 `pytest -q` 与 `npm run build`。
- [x] 使用 `fixtures/sample_balance_rules.md` 和至少两份重叠规则文档验证增量更新。
- [x] 验证旧任务引用仍可打开；无法迁移的引用显示 legacy 标记。
- [x] 记录迁移耗时、失败恢复和回滚步骤。
- [x] 更新 README、`.env.example` 和 API 文档。

## 建议提交边界

```text
feat: add governed wiki page schema
feat: migrate wiki pages to revisioned storage
feat: queue persistent wiki ingest jobs
feat: plan incremental wiki updates
feat: apply reviewed wiki revisions atomically
feat: add wiki lint and maintenance files
feat: add FTS-backed wiki retrieval
feat: add wiki review workflow
```

## 风险控制

- 每个阶段先兼容旧 API，再切换前端。
- 数据迁移和页面应用必须可重复、可回滚。
- 不在同一提交同时更换数据模型、Prompt、检索算法和 UI。
- 未建立检索评测集前，不以“模型感觉更好”作为验收依据。
- 不复制 `nashsu/llm_wiki` GPLv3 代码；只实现本规格定义的独立领域方案。
