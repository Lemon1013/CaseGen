# CaseGen Wiki Spaces 实施计划

**规格：** `docs/superpowers/specs/2026-08-09-wiki-spaces-design.md`
**目标：** 在保留现有 Wiki 2.0 能力和历史数据的前提下，实现按项目空间隔离的文档摄入、Wiki、审核、检索和生成任务。

## Task 1：空间模型与安全迁移

**主要文件：** `models/entities.py`、`db.py`、`services/wiki_migrate.py`、迁移测试。

- 新增 `WikiSpace` 和空间状态。
- 为 Document、IngestJob、SourceChunk、WikiPageRow、WikiReviewItem、GenerationTask 增加空间字段。
- 创建并回填默认空间。
- 将 page_key 唯一约束改为 `(space_id, page_key)`。
- 增加常用 `(space_id, status)`、`(space_id, document_id)` 索引。
- 覆盖空库、旧库、重复执行和部分迁移场景。

## Task 2：空间 API 与领域校验

**主要文件：** 新增 `api/wiki_spaces.py`、空间 schema/service，更新 `main.py`。

- 实现列表、详情、创建、更新和归档接口。
- slug 规范化并禁止路径穿越。
- 返回文档数、页面数、待审核数和最后更新时间。
- 归档空间拒绝写操作；非空空间不物理删除。

## Task 3：文件仓储与派生文件隔离

**主要文件：** `wiki_repository.py`、`wiki_staging.py`、index/lint/graph 相关服务。

- Repository 显式接收 `space_id`。
- 页面路径改为 `spaces/{slug}/pages/...`。
- 锁键改为 `space_id + page_key`。
- index、overview、log 和 lint 逐空间生成。
- 将旧文件迁移到默认空间，并验证回滚安全。

## Task 4：文档与摄入链路

**主要文件：** `api/documents.py`、`wiki_jobs.py`、`wiki_ingest.py`、`wiki_apply.py`、SourceChunk 服务。

- 上传和文档列表支持空间。
- Job 固化空间快照，摄入各阶段只读取 Job 空间。
- 候选页、Step A/B、Review 和 WikiPageSource 严格同空间。
- 摄入指纹加入空间，重复检测限定在空间内。
- API 响应暴露空间信息。

## Task 5：FTS 与混合检索隔离

**主要文件：** `wiki_fts.py`、`retrieve.py`、`source_chunks_store.py`、`hybrid_retrieve.py`。

- 所有加载和搜索函数显式要求 `space_id`。
- FTS 投影保存空间 ID，按空间计数、重建和查询。
- Wiki、SourceChunk、条款锚点和链接扩展都只使用当前空间。
- 新增同名页面、同条款、无命中 fallback 的跨空间泄漏测试。

## Task 6：任务创建与生成 Pipeline

**主要文件：** `schemas/tasks.py`、`api/tasks.py`、`task_pipeline.py`。

- TaskCreate/TaskOut 增加 Wiki 空间。
- 创建任务校验空间活动状态并保存快照。
- 生成、重生成和评审复用任务空间。
- TaskEvent 和任务详情显示空间；旧任务归入默认空间。

## Task 7：前端空间工作流

**主要文件：** 新增空间 API/View；更新布局、路由、Documents、Wiki、WikiReview、Workbench、TaskDetail。

- 新增 Wiki 空间管理入口。
- 文档上传、浏览、审核增加空间上下文。
- 工作台必选活动空间并提交 `wiki_space_id`。
- 任务详情只读展示空间。
- 使用路由 query 保持当前空间，切换空间清空旧详情。

## Task 8：回归与验收

- 更新 API smoke 和现有 fixture，使默认空间兼容旧测试。
- 后端新增空间 CRUD、迁移、摄入一致性、Repository 同名 key 和严格检索隔离测试。
- 前端运行 `npm run build`。
- 完整运行 `pytest -q`。
- 使用两个空间、两份同主题文档和同名 page_key 做一次本地端到端验收。

## 完成定义

- 规格中的七项验收标准全部可由自动化测试或本地验收证明。
- 不修改模型/Prompt 的全局语义。
- 不允许任何代码路径在缺少空间时静默检索全库；兼容逻辑只能显式解析到默认空间。
- 迁移前备份、迁移后 FTS 重建和服务启动均成功。
