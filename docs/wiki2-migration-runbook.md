# Wiki 2.0 迁移、恢复与发布手册

本文档针对当前 CaseGen 代码。迁移不是 Alembic 命令：应用初始化时调用 `init_db()`，再由 `migrate_wiki_schema()` 完成 SQLite Wiki 2.0 迁移。所有演练先针对 `APP_DATA_DIR` 的副本，不直接在生产 `data/` 上试跑。

## 1. 数据边界与停机要求

默认数据根目录是仓库根下的 `data/`；设置 `APP_DATA_DIR` 后，以下路径都相对于该目录：

```text
data/
├── meta/app.db          # SQLite：页面元数据、Job、Review、revisions、模型配置
├── wiki/                # purpose.md、schema.md、index.md、pages/*.md
└── raw/sources/         # 上传原文
```

迁移、备份和恢复前停止后端 Uvicorn 进程，并确认没有摄入 Job 正在写库或替换 Markdown。备份必须覆盖整个数据根目录，而不是只复制 `app.db`：数据库中的 `path`、页面 revision 和原文块引用需要与 `wiki/`、`raw/` 同步。

`app.db` 中的模型配置包含 `api_key`；上传原文、Wiki Markdown、Job 日志和备份可能包含客户规则或业务数据。当前应用没有认证/RBAC，CORS 允许 `*`，因此不要把后端端口直接暴露到不受信网络。备份应使用受限目录权限和组织批准的加密存储；不要提交 `data/`、`.env`、数据库或备份文件。若数据库或备份泄露，应立即轮换模型 API Key。

## 2. 迁移前备份与副本演练

仓库提供只读源目录的验证脚本。输出放在已被 Git 忽略的 `data-backups/`，且目标目录必须不存在：

```powershell
Set-Location .\backend
python scripts\verify_wiki2_migration.py `
  --source-data ..\data `
  --output ..\data-backups\wiki2-release-YYYYMMDD-HHMMSS
```

脚本复制完整数据后，只对副本执行 `init_db()` 和 FTS 重建，并输出源数据库前后 SHA256、耗时、`PRAGMA integrity_check`、外键违规数与索引计数到副本中的 `wiki2-migration-report.json`。若输出目录已存在、位于源目录内部、完整性失败或源哈希变化，脚本会失败退出。它不会迁移 Markdown 文件，也不会替换原始上传文件。

本次发布演练（2026-08-03）记录：完整数据复制 `0.0104s`，迁移及 FTS 重建 `0.1308s`，SQLite 完整性 `ok`，外键违规 `0`，源数据库迁移前后 SHA256 均为 `A90783D12BE8A77291E3371700729F7020B844C16C219645287AF3FB10D81162`。

### 自动迁移做什么

启动时检测到旧或部分迁移的 `wiki_pages`、`ingest_jobs`、`source_chunks` 表时，代码会在任何 DDL/backfill 前创建一次：

```text
<APP_DATA_DIR>/meta/app.db.wiki2-pre-migration.bak
```

该文件只在不存在时创建，后续重复启动不会覆盖它。迁移随后会：

1. 增加 Wiki 2.0 页面、Job 和 SourceChunk 字段。
2. 创建 `wiki_page_sources`、`wiki_page_revisions`、`wiki_review_items` 等表及查询索引。
3. 为没有稳定 key 的旧页面回填 `legacy.page.<id>`（发生冲突时追加 `.legacy1` 等后缀）。
4. 将旧页面默认设为 `published`、`revision=1`，保留原 `id` 和 Markdown `path`。

新数据库或已经完整迁移的数据库不会再次生成迁移备份。由于固定备份名不会覆盖旧备份，每次发布仍应先做带时间戳的完整 `data/` 备份。

## 3. FTS5 索引重建与验证

Wiki 页面和 SourceChunk 写入时会尽力增量更新 FTS5 投影；FTS5 不可用或写入失败不会阻断 canonical 页面/原文写入。检索会检查投影行数，发现数量不一致时自动重建；查询失败则返回 `retrieval_mode=heuristic_fallback`。当前没有公开的 FTS 重建 HTTP 端点；`scripts/verify_wiki2_migration.py` 可在完整数据副本上执行全量重建和校验。

如果需要在迁移副本上强制全量重建，可在已设置 `APP_DATA_DIR` 且当前目录为 `backend/` 的 Python 交互环境中调用现有服务函数：

```text
>>> from app.db import get_engine, init_db
>>> from sqlmodel import Session
>>> from app.services.retrieve import load_all_wiki_pages
>>> from app.services.source_chunks_store import load_all_source_chunks
>>> from app.services.wiki_fts import rebuild_fts
>>> init_db()
>>> with Session(get_engine()) as session:
...     result = rebuild_fts(
...         session,
...         load_all_wiki_pages(session),
...         load_all_source_chunks(session),
...     )
...     print(result)
```

重建后用 `GET /api/wiki/pages` 和 `POST /api/wiki/retrieve` 验证页面与命中；响应中的 `retrieval_mode` 应为 `fts5_hybrid`（若运行环境 SQLite 未编译 FTS5，则保留 `heuristic_fallback`）。不要直接编辑 FTS 表，canonical 数据源仍是 SQLModel 表和 Markdown 文件。

## 4. 发布回归清单

在迁移副本上完成并记录以下证据：

- [ ] 记录完整 `data/` 备份目录、`app.db` SHA256、备份时间和迁移耗时。
- [ ] 启动副本后访问 `GET /api/health`、`GET /api/wiki/pages` 和 `GET /api/wiki/index`。
- [ ] 用 `fixtures/sample_balance_rules.md` 上传、预览、摄入，并轮询 `GET /api/ingest-jobs/{job_id}` 到 `success`/`failed`。
- [ ] 再准备至少两份有重叠条款的真实规则文档，分别摄入；检查相同主题页面产生 `update`/`noop` 或 Review，而不是无依据地重复建页。
- [ ] 用一个含明确条款号的查询调用 `POST /api/wiki/retrieve`，核对 `source_chunk_id`、`start_char`、`end_char`、`clause_ids` 和 `anchored_clause_ids` 能回到原文。
- [ ] 对产生的 pending Review 走 `GET detail → Diff → approve/reject`；确认批准会产生新 revision，拒绝不会发布候选。
- [ ] 对一个非最新 revision 做回滚，确认历史未删除，新增记录的 `operation` 为 `rollback`，并重新检索确认结果已变化。
- [ ] 检查旧任务引用：Wiki 引用通过 `GET /api/wiki/pages/{page_id}`，原文引用通过 `GET /api/source-chunks/{chunk_id}`；旧页面若被迁移应能看到 `page_key=legacy.page.<id>` 标记。
- [ ] 在 `backend/` 执行 `pytest -q`，在 `frontend/` 执行 `npm run build`。

仓库已固化 `fixtures/sample_balance_rules.md`、`fixtures/overlap_balance_rules_v1.md` 和 `fixtures/overlap_balance_rules_v2.md` 三份无敏感数据的回归样例。客户原文不得作为 fixture 提交；额外真实文档只记录脱敏后的验证结论和 SHA256。

## 5. 摄入失败、重试与重启恢复

按以下顺序处理，不要重复点击导致并发摄入：

1. 用 `GET /api/ingest-jobs/{job_id}` 查看 `status`、`stage`、`progress`、`step_log_json` 和 `error_message`；同时查看文档记录的 `status`/`error_message`。
2. `stage=failed` 时先按错误修复：原文解析质量问题看 `/api/documents/{document_id}/preview`，模型/Prompt 问题先修复模型配置或激活 Prompt，文件缺失则恢复 `raw/sources/`。
3. 修复后调用 `POST /api/documents/{document_id}/ingest?force=true`。活动 Job 仍存在时，接口会返回已有 Job；不要再创建第二个 Job。
4. 若进程在 `queued`/`running` 期间退出，下一次应用初始化会把没有活动 worker 的 `running` Job 重置为 `queued`，追加 `recovered` 步骤并重新调度。
5. 若运行中的 LLM 调用卡住，`POST /api/ingest-jobs/{job_id}/cancel` 只会设置取消标志；请求返回后在下一边界结束。排队 Job 会立即取消。取消后需要新的 `force=true` Job 才能重新摄入。

页面仓储的单页 create/update/rollback 会先在 staging 目录写入并二次校验，再原子替换正式 Markdown；失败时会回滚数据库并尝试恢复旧文件。摄入 Job 本身失败会被记录为 `failed`，但已完成的单页应用会保留其 revision/Review 审计；需要纠正已发布页面时使用页面级 rollback，不要手工改数据库。

## 6. 页面回滚与整库恢复

### 页面级回滚（优先）

适用于某一页面内容错误但数据库和其他页面仍健康的情况：

1. `GET /api/wiki/pages/{page_id}/revisions`，选择已验证的 `revision` 或 `revision_id`。
2. `POST /api/wiki/pages/{page_id}/rollback`，请求体只能二选一提供 `revision`/`revision_id`，并填写非空 `reason`。
3. 检查返回 revision 的 `operation=rollback`，再读取页面、Diff 和检索结果。

回滚是立即发布操作，不经过 Review；它保留全部历史并创建新 revision。若返回 404，先确认 page/revision 属于同一页面；若返回 422，检查历史 frontmatter、请求体是否同时/都未提供目标，或 `job_id` 是否存在。

### 整库恢复

适用于迁移失败、数据库与 Markdown 不一致或误操作影响多个页面的情况。优先恢复发布前的完整 `data/` 副本：

1. 停止后端和所有会访问该数据根目录的 worker，保留当前故障目录，不要覆盖或删除它。
2. 将完整备份复制到新的恢复目录，并把部署配置的 `APP_DATA_DIR` 指向该恢复目录；先用 `/api/health` 和只读页面/检索请求验证。
3. 若只剩自动生成的 `app.db.wiki2-pre-migration.bak`，它只能恢复数据库的迁移前状态；必须同时找到同一时间点的 `wiki/` 与 `raw/` 文件副本，不能只替换数据库后直接上线。
4. 恢复验证通过后再切换服务配置；保留故障目录和原备份，按组织变更流程确认后再清理。

恢复到新目录可避免破坏现场。PowerShell 的基本操作形式如下，路径必须先核对为本次备份和恢复目录：

```powershell
$restore = "data-restore-wiki2"
New-Item -ItemType Directory -Force $restore
Copy-Item -Path ".\data-backup-20260803-120000\*" -Destination $restore -Recurse -Force
$env:APP_DATA_DIR = (Resolve-Path .\$restore).Path
Set-Location .\backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

示例中的时间戳必须替换为实际备份目录；恢复验证完成前不要执行递归删除。若使用固定部署环境变量而不是当前 PowerShell 会话，请同步更新服务配置并重启后端。

## 7. 发布记录模板

每次迁移至少记录：发布版本、操作者、数据根目录、完整备份路径与 SHA256、迁移前后页面/SourceChunk/Review 数量、迁移耗时、FTS `retrieval_mode`、失败 Job 及重试结果、页面回滚 revision，以及最终 `/api/health`、`pytest -q`、`npm run build` 结果。记录中不要写入 API Key、完整客户原文或包含密钥的 `app.db` 内容。
