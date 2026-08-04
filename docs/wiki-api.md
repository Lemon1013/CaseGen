# Wiki 2.0 API

本文档以当前 FastAPI 路由和 Pydantic Schema 为准。API 前缀为 `/api`，默认地址为 `http://127.0.0.1:8000`。时间字段使用 JSON datetime；请求体未知字段会被部分 Schema 拒绝。Swagger 可在 `/docs` 查看。

## 1. 文档摄入与原文块

### 上传与质量预览

`POST /api/documents` 使用 `multipart/form-data`，字段名为 `file`。当前支持 `.md`、`.txt`、`.pdf`、`.docx`，单文件上限为 20 MiB。返回 `DocumentOut`：`id`、`filename`、`stored_path`、`sha256`、`status`、`char_count`、`error_message` 和时间字段。上传时会同步解析一次；解析失败仍会保存文档记录并将状态设为 `failed`。

`GET /api/documents` 列出文档；`GET /api/documents/{document_id}` 获取单个文档。

`GET /api/documents/{document_id}/preview?max_chars=50000` 重新解析上传原文并返回 `DocumentPreviewOut`。`max_chars` 范围为 500–200000，响应包含 `text`、`char_count`、`returned_chars`、`truncated`、`quality_ok` 和 `diagnostics`。质量诊断可能包含空文本、替换字符率、疑似扫描 PDF、页数、警告和错误。`quality_ok=false` 时摄入会在解析阶段失败。

### 创建和跟踪摄入 Job

`POST /api/documents/{document_id}/ingest?force=false` 创建摄入 Job，成功返回 `IngestJobOut`。生产路径只入队并立即返回，后台单 worker 执行；同一文档已有 `queued`/`running` Job 时直接返回该 Job。若已有成功 Job 且源文件 SHA256、Wiki 配置和激活 Prompt 指纹未变化，则复用原 Job；需要重新执行时使用 `force=true`。

Job 状态为 `queued`、`running`、`success`、`failed` 或 `cancelled`；阶段包括 `queued`、`parsing`、`chunking`、`analyzing`、`planning`、`writing`、`indexing`、`ready`、`failed`、`cancelled`。`progress` 为 0–100，`step_log_json` 保存结构化步骤日志，`plan_json` 保存摄入指纹、候选页 key、窗口结果和 Step A 计划。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/ingest-jobs?status=queued,running&document_id=1` | 按状态（可逗号分隔）或文档列出 Job |
| `GET` | `/api/ingest-jobs/{job_id}` | 查询 Job 详情、进度和错误 |
| `POST` | `/api/ingest-jobs/{job_id}/cancel` | 请求取消 Job |

排队中的 Job 会立即变为 `cancelled`；运行中的 Job 只设置取消标记，在当前 LLM 请求返回、下一阶段或窗口边界停止，不能强制中断已经发出的 HTTP 请求。应用重启时，孤立的 `running` Job 会被恢复为 `queued` 并重新调度。

### 原文块

`GET /api/documents/{document_id}/chunks` 返回该文档的 `SourceChunkOut` 列表，包含 `chunk_index`、`text`、`start_char`、`end_char`、页码、章节、`clause_ids_json` 和父块索引。

`POST /api/documents/{document_id}/rechunk` 只重新解析并重建原文块，不重新调用 LLM。`GET /api/source-chunks/{chunk_id}` 获取单个原文块。重分块会更新原文检索投影；原文块是无损引用层，Wiki 页面不是原文的替代品。

## 2. Wiki 页面与检索

`GET /api/wiki/pages` 返回页面元数据列表；当前路由不接收筛选参数。`GET /api/wiki/pages/{page_id}` 返回页面及 Markdown 正文。页面字段包括 `page_key`、`page_type`、`domain`、`status`、`revision`、`aliases`、`tags`、`source_document_id` 和时间字段。允许的页面类型是 `source`、`rule`、`entity`、`scenario`、`regression`、`synthesis`；归档页面状态为 `archived`。

`GET /api/wiki/index` 返回生成的 `index.md` 内容和路径。Index、Overview、Log 和 Lint 是本地维护文件/服务产物，不提供单独的 HTTP 修改接口。

### 混合检索

`POST /api/wiki/retrieve` 请求体：

```json
{
  "query": "集合竞价成交价格如何确定？",
  "top_k": 10,
  "types": ["rule"]
}
```

`query` 必填；`top_k` 省略时使用 `RETRIEVE_TOP_K`；`types` 只过滤 Wiki 页面类型。响应 `RetrieveResponse` 包含：

- `hits`：按融合分数排序的 Wiki 页面和原文块；`citation_type` 为 `wiki` 或 `source`。
- `wiki_hit_count`、`source_hit_count`：两类结果数量。
- `clause_ids`：查询及命中 Wiki 文本中识别出的条款号。
- `anchored_clause_ids`：仅由查询文本明确写出的条款号产生的强原文锚点。
- `retrieval_mode`：通常为 `fts5_hybrid`；SQLite FTS5 不可用或查询失败时为 `heuristic_fallback`。
- `explain` 与每个 hit 的 `explain`：包含 BM25/RRF、命中字段、排名或一跳扩展原因等可选诊断信息。

FTS5 对标题、别名、标签、条款号和正文加权，并与确定性启发式结果做 Reciprocal Rank Fusion；高分 Wiki 页面可通过 wikilink 或共享来源扩展一跳。FTS 是可重建投影，降级到 `heuristic_fallback` 不代表 canonical 页面或原文丢失。Wiki hit 的 `content` 通常不放入检索响应，需要用页面接口取全文；source hit 的 `content` 是原文块摘录，完整块用 `GET /api/source-chunks/{chunk_id}` 获取。

仅当查询本身包含条款号（例如 `3.5.2`）时，系统才把匹配原文块标记为强锚点；不能把 Wiki 推断出的条款号当作同等强度的原文依据。

## 3. Review、Diff 与 Revisions

### Review

| 方法 | 路径 | 查询/请求体 |
|---|---|---|
| `GET` | `/api/wiki/reviews` | `status`、`kind`、`page_id`、`job_id` 可选 |
| `GET` | `/api/wiki/reviews/{review_id}` | 返回候选内容、旧版本、理由、风险标记、来源证据和 Diff |
| `POST` | `/api/wiki/reviews/{review_id}/approve` | `{"reviewed_by":"alice","decision_reason":"..."}` |
| `POST` | `/api/wiki/reviews/{review_id}/reject` | 同上；也可使用 `reason` |

Review 状态为 `pending`、`approved`、`rejected`。批准或拒绝只接受 `pending`，重复处理返回 409。`approve` 会通过页面仓储原子写入 Markdown、数据库 revision、来源关系和检索投影；`create`/`update` 候选可以批准，`merge` 候选当前接口明确返回 409，不能盲目重试。`reject` 保留审核记录，不发布候选内容。

Review 详情的 `source_evidence` 使用 `{document_id, chunk_ids, clauses}`；`diff` 使用 `from_revision`、`to_revision`、`unified`/`text` 和 `changed`。

### Revisions 与 Diff

`GET /api/wiki/pages/{page_id}/revisions` 按 revision 列出页面历史；`GET /api/wiki/pages/{page_id}/revisions/{revision_id}` 获取一个历史版本，内容字段为 `content_md`，元数据字段为 `frontmatter`/`frontmatter_json`，并带有 `operation`、`job_id`、`reason`。

`GET /api/wiki/pages/{page_id}/diff?from_revision=1&to_revision=2` 返回统一 Diff。省略 `from_revision` 时默认使用倒数第二个版本（只有一个版本时使用它），省略 `to_revision` 时使用最新版本。

## 4. Rollback

`POST /api/wiki/pages/{page_id}/rollback` 必须且只能提供 `revision_id` 或 `revision` 其中一个：

```json
{
  "revision": 1,
  "reason": "恢复到已验证的规则版本",
  "reviewed_by": "alice"
}
```

`job_id` 可选，但如果提供必须是现有摄入 Job。回滚读取历史 frontmatter 和正文，通过同一页面仓储生成新的 `operation=rollback` revision；不会删除中间历史，也不复用旧 revision 号。成功后返回新建的 `WikiRollbackOut`，并尽力重建 Index、Overview 和检索投影。

建议顺序：先 `GET revisions` 确认目标 → 执行 rollback → 再 `GET page`、`GET revisions` 和 `POST retrieve` 验证。回滚是立即发布操作，当前没有额外 Review 阶段；无效页面、历史版本或目标参数分别返回 404/422。

## 5. 常见错误与安全边界

`GET /api/tasks/{task_id}/citations` 对旧任务保持兼容。每条引用增加 `available`、`legacy` 和 `legacy_reason`：目标 Wiki 页面或 SourceChunk 仍存在时 `available=true`；目标无法关联时返回 `legacy=true`，前端展示任务保存的历史摘录，不会把失效路径伪装成可打开链接。

`404` 表示文档、Job、页面、审核项、revision 或原文块不存在；`409` 表示重复处理、并发状态不允许或 merge 候选不能自动批准；`422` 表示候选/历史内容不符合 Wiki Schema、回滚参数不完整或提供了未知字段；上传扩展名/大小错误通常返回 `400`。

当前 API 没有用户认证和角色权限控制，应用还允许跨域 `*`。不要把后端端口直接暴露到不受信网络；应由内网、防火墙或已认证的反向代理保护 Review、模型配置和上传接口。
