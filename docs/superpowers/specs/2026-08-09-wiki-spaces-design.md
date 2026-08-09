# CaseGen Wiki Spaces 设计规格

**日期：** 2026-08-09
**状态：** 已批准实施
**目标：** 将全局 Wiki 拆分为按项目隔离的 Wiki Space；文档在指定空间摄入，生成任务只能检索创建时选择的空间。

## 1. 设计结论

Wiki Space 必须是贯穿数据库、文件、摄入、审核、FTS 和生成任务的一等隔离键，不能只依靠前端筛选或文件夹分组。首期采用“一个文档属于一个空间、一个任务选择一个空间”的模型；模型配置和提示词继续全局共享。

核心约束：

1. 不同空间允许相同 `page_key`，同一空间内仍保持唯一。
2. 检索、链接扩展和原文召回不得在无结果时回退到其他空间。
3. 文档完成摄入后不可直接跨空间移动；需要在目标空间重新上传或重新摄入。
4. 生成任务保存 `wiki_space_id` 快照，创建后不可修改，以保证引用可复现。
5. 旧数据自动归入系统创建的“默认空间”，迁移不丢失页面、revision、审核项和引用。

## 2. 数据模型

新增 `WikiSpace`：

```text
WikiSpace
  id            INTEGER PK
  name          VARCHAR NOT NULL
  slug          VARCHAR NOT NULL UNIQUE
  description   TEXT DEFAULT ''
  status        VARCHAR DEFAULT 'active'   # active | archived
  created_at    DATETIME
  updated_at    DATETIME
```

以下实体增加空间字段：

| 实体 | 字段 | 说明 |
|---|---|---|
| `Document` | `space_id` | 文档归属；上传后固定 |
| `IngestJob` | `space_id` | 摄入时快照，避免文档状态变化影响历史 Job |
| `SourceChunk` | `space_id` | 原文检索快速过滤 |
| `WikiPageRow` | `space_id` | Wiki 页面归属 |
| `WikiReviewItem` | `space_id` | 审核列表直接按空间过滤 |
| `GenerationTask` | `wiki_space_id` | 任务检索边界，创建后不可修改 |

`WikiPageRevision`、`WikiPageSource` 和 `TaskCitation` 可通过父实体确定空间，不重复存储；写入服务必须校验页面、文档、Job 和空间一致。

将 `wiki_pages.page_key` 的全局唯一索引替换为：

```sql
CREATE UNIQUE INDEX uq_wiki_pages_space_page_key
ON wiki_pages(space_id, page_key)
WHERE page_key IS NOT NULL;
```

同一 SHA256 文档允许存在于不同空间；空间内重复检测使用 `(space_id, sha256)`。

## 3. 文件布局

全局 `schema.md` 和默认 Prompt 继续共享，正式页面按空间落盘：

```text
data/wiki/
  schema.md
  purpose.md
  spaces/
    default/
      index.md
      overview.md
      log.md
      pages/
        sources/
        rules/
        entities/
        scenarios/
        regressions/
        synthesis/
    project-a/
      index.md
      overview.md
      log.md
      pages/...
```

数据库 `wiki_pages.path` 保存相对 `data/wiki/` 的路径，例如 `spaces/project-a/pages/rules/order.limit.md`。所有路径由 `WikiRepository` 根据 `space.slug + page_type + page_key` 生成，API 和 LLM 不得提供磁盘路径。

写锁从 `page_key` 改为 `(space_id, page_key)`；摄入指纹必须包含 `space_id`。

## 4. 服务边界

### 4.1 空间服务

新增集中式空间解析服务，提供：

- 获取活动空间；
- 获取/创建默认空间；
- 校验空间状态；
- 解析空间根目录；
- 返回空间统计（文档、页面、待审核数和最后更新时间）。

归档空间只读：保留历史任务和引用，但禁止上传、摄入和新建任务。非空空间首期不支持物理删除。

### 4.2 摄入

上传文档时记录 `space_id`；创建 Job 时复制到 `ingest_jobs.space_id`。解析、分块、候选页召回、Step A、Step B、应用、派生索引和审核项全部使用 Job 的空间，不从请求再次读取。

候选页只能从当前空间召回。`WikiPageSource` 写入前必须确认来源文档与目标页面同空间。

### 4.3 检索

`hybrid_retrieve()`、`load_all_wiki_pages()`、`load_all_source_chunks()` 和 FTS 查询全部要求 `space_id`。FTS5 投影增加 `space_id UNINDEXED`，查询同时使用 `MATCH` 和空间等值过滤；计数、重建和增量更新按空间执行。

页面 `[[page_key]]` 只在当前空间解析。无命中时可以退回当前空间的启发式检索，但禁止退回全局数据。

### 4.4 生成任务

创建任务必须选择活动 `wiki_space_id`。Pipeline 从任务读取空间并传给混合检索；任务事件和输出返回空间信息。已有任务迁移到默认空间。

模型和 generate Prompt 仍由任务单独选择，与 Wiki Space 正交。

## 5. API 契约

新增：

```text
GET    /api/wiki-spaces
POST   /api/wiki-spaces
GET    /api/wiki-spaces/{id}
PUT    /api/wiki-spaces/{id}
POST   /api/wiki-spaces/{id}/archive
```

调整：

- `POST /api/documents`：multipart 增加 `space_id`；兼容期缺省归入默认空间。
- `GET /api/documents`：支持 `space_id` 查询参数，响应包含 `space_id/space_name`。
- `POST /api/tasks`：增加必填 `wiki_space_id`；兼容旧客户端时由后端填默认空间。
- `GET /api/wiki/pages|index|reviews`：要求或支持 `space_id`。
- `POST /api/wiki/retrieve`：请求体增加 `space_id`。
- Job、Page、Review、Task 响应均返回空间标识。

服务端是最终隔离边界。即使客户端篡改页面 ID，也要校验资源属于当前请求/任务空间。

## 6. 前端交互

1. 新增“Wiki 空间”管理页：列表、统计、新建、编辑、归档。
2. 文档管理顶部增加空间选择器；上传弹窗必须确认目标空间。
3. Wiki 浏览和 Wiki 审核使用同一空间筛选语义，切换后清空当前选中项。
4. 工作台新增必填“Wiki 空间”，默认选中上次使用的活动空间；选项展示文档数、页面数和更新时间。
5. 任务详情显示空间名称，不能修改。
6. 空间 ID 放入路由查询参数，刷新页面后筛选不丢失；localStorage 只记录最近选择，不作为权限或隔离依据。

## 7. 迁移与兼容

迁移顺序：

1. 在任何 DDL 前备份 SQLite。
2. 创建 `wiki_spaces` 和默认空间。
3. 以可空字段方式增加各实体的空间列并回填默认空间。
4. 删除旧 page_key 唯一索引，创建复合唯一索引和空间查询索引。
5. 将现有 Wiki 文件移动到 `spaces/default/`，更新数据库路径；失败时回滚文件和数据库。
6. 重建 FTS 投影和默认空间 index/overview/log。
7. 将空间列收敛为业务层必填；旧 API 在兼容期自动使用默认空间并记录警告。

迁移必须可重复执行，并支持旧库、部分迁移库和空库。

## 8. 验收标准

1. 空间 A/B 可各自创建相同 `page_key`，同空间重复仍失败。
2. A 的任务不会检索到 B 的 Wiki 页面或 SourceChunk，包括 FTS、启发式和链接扩展。
3. 文档摄入产生的 Page、Source、Review、Job 全部属于同一空间。
4. 默认空间迁移后，旧页面、历史 revision、审核项和任务仍可访问。
5. 归档空间不可上传、摄入或创建任务，但历史内容可读。
6. 前端所有 Wiki 相关页面刷新后仍保持空间上下文。
7. 完整 `pytest`、`npm run build` 通过，并新增跨空间泄漏回归测试。

## 9. 非目标

- 首期不实现一个任务联合检索多个空间。
- 首期不实现用户/角色级空间权限。
- 首期不实现空间级模型、Prompt 或 schema 覆盖。
- 首期不支持将已摄入文档直接移动到另一空间。
