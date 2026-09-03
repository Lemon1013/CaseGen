# LLM 知识库 API 说明

所有接口前缀 `/api/`。鉴权使用 Django session cookie：登录后所有请求需 `credentials: "include"`（浏览器）或 `-b cookies.txt`（curl）。

## 账户与权限

### 角色说明

| 角色 | 能力 |
| --- | --- |
| 匿名 | 检索问答、关系图谱、查看文档内容、查看领域知识层 |
| 普通用户 | 同匿名（登录后可标识身份） |
| 管理员（is_staff） | 全部能力 + 知识摄入、登记/移除本地项目、查询摄入任务 |

创建管理员账户：

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

### POST /api/auth/login/

登录并写入 session cookie。

请求体：

```json
{ "username": "admin", "password": "your-pass" }
```

成功 200：

```json
{
  "authenticated": true,
  "username": "admin",
  "isAdmin": true,
  "isSuperuser": true,
  "userId": 1
}
```

失败 401：

```json
{ "error": "用户名或密码错误" }
```

### GET /api/auth/me/

返回当前会话身份。匿名也可调用。

未登录 200：

```json
{ "authenticated": false, "isAdmin": false, "username": "" }
```

已登录 200：

```json
{
  "authenticated": true,
  "username": "admin",
  "isAdmin": true,
  "isSuperuser": true,
  "userId": 1
}
```

### POST /api/auth/logout/

登出当前 session。无请求体。

```json
{ "ok": true }
```

## 基础接口（匿名可用）

### GET /api/health/

健康检查。

```json
{
  "portal": "ok"
}
```

### GET /api/projects/

列出 Django 数据库中登记的本地项目。

```json
{
  "projects": [
    { "id": "2886e610-...", "name": "法规库", "path": "D:/...", "rawDir": "D:/.../sources", "exportDir": "D:/.../exports/wiki" }
  ]
}
```

### GET /api/projects/{project_id}/files/

列出项目内文件树。

```json
{ "items": [ { "path": "raw/sources/rules.docx", "name": "rules.docx" }, { "path": "exports/wiki/index.md", "name": "index.md" } ] }
```

### GET /api/projects/{project_id}/graph/

从 KnowledgeObject、Claim 和 ObjectSource 返回有向知识图谱。

```json
{
  "nodes": [ { "id": "object:obj_xxx", "label": "认购邀请书", "type": "document", "path": "" } ],
  "links": [ { "source": "object:obj_xxx", "target": "object:obj_yyy", "type": "适用于", "claimId": "clm_xxx", "directed": true, "evidenceCount": 1 } ]
}
```

### GET /api/projects/{project_id}/file-content/?path=raw/sources/rules.docx

读取单个文件正文。

```json
{ "path": "raw/sources/rules.docx", "content": "..." }
```

## 检索与问答

### POST /api/projects/{project_id}/search/

仅检索，不调用 LLM。

请求体：

```json
{
  "query": "认购邀请书的作用",
  "limit": 8,
  "useSynonyms": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户问题 |
| `limit` / `topK` | 否 | 结果数量，默认 8 |
| `useSynonyms` | 否 | 是否启用 synonym.yaml 扩展，默认 true |

返回：

```json
{
  "results": [
    {
      "path": "raw/sources/接口说明.docx",
      "chunkId": 183,
      "documentId": 12,
      "documentVersionId": 19,
      "structuralNodeId": "nod_xxx",
      "documentPath": "交易规则/接口说明.docx",
      "headingPath": "竞价交易 > 订单确认消息",
      "pageNo": 12,
      "title": "...",
      "score": 12,
      "snippet": "...",
      "evidenceSnippet": "...",
      "evidenceKind": "source",
      "evidenceSourcePath": "D:/.../xxx.docx",
      "highlightTerms": ["认购邀请书", ...],
      "llmRelevance": 0.82,
      "llmRelevanceReason": "...",
      "matchKind": "exact-object",
      "matchedTerms": ["竞价订单确认消息"]
    }
  ],
  "synonymExpansion": {
    "query": "认购邀请书的作用",
    "expandedQuery": "认购邀请书的作用 认购邀请书",
    "expandedTerms": ["认购邀请书"],
    "matchedGroups": [ { "canonical": "认购邀请书", "terms": [...] } ]
  },
  "searchContextId": "abc123...",
  "searchIndex": {
    "ok": true,
    "indexVersion": 6,
    "retrievalMode": "object-linked",
    "globalFtsUsed": false,
    "objectLinkedChunks": 4,
    "linkedSourceDocuments": 1,
    "fallbackReason": "",
    "elapsedMs": 5.2
  },
  "queryPlan": {
    "strategy": "exact-first",
    "exactObjects": ["竞价订单确认消息"],
    "exactPhrases": [],
    "fallbackTerms": ["竞价", "订单", "确认消息"]
  },
  "highlightTerms": ["认购邀请书", ...],
  "llmRerank": { "enabled": false, "reason": "未启用" }
}
```

`matchKind` 可能为：`object-source-structured`（ObjectMention 结构节点直连）、`object-source-exact`（对象名称/别名直连）、`object-source-evidence`、`exact-object`、`exact-title`、`exact-phrase` 或 `fulltext`。

### POST /api/projects/{project_id}/compose/

**核心问答接口**：检索 → 用 chunkId 定位 V7 最深章节 → 读取完整章节及祖先公共正文 → 调用 LLM 生成带引用的回答。

请求体：

```json
{
  "query": "上市公司再融资中认购邀请书的作用是什么？",
  "limit": 6,
  "maxDocs": 8,
  "paths": [],
  "chunkIds": [183, 194, 207],
  "searchContextId": "abc123...",
  "useSynonyms": true,
  "useIntent": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户问题 |
| `paths` | 否 | 指定文档路径列表；为空时自动检索选 topK |
| `chunkIds` | 否 | `/search/` 返回的切片 ID；用于定位所属最深章节，不直接把摘要或单个切片作为回答证据 |
| `searchContextId` | 否 | `/search/` 返回的短期上下文 ID，用于复用查询意图和改写结果 |
| `maxDocs` | 否 | 拼接给 LLM 的文档上限，默认 8 |
| 其他字段 | 否 | 同 `/search/` |

返回（精简）：

```json
{
  "query": "上市公司再融资中认购邀请书的作用是什么？",
  "answer": "根据《上海证券交易所上市公司证券发行与承销业务实施细则》第三十三条……[1][2]",
  "model": "deepseek-v4-flash",
  "llmConfigured": true,
  "usage": { "prompt_tokens": 1234, "completion_tokens": 567 },
  "documents": [
    { "path": "raw/sources/xxx.docx", "chunkId": 183, "chunkIds": [183, 194], "sectionId": "nod_...", "headingPath": "第三章 > 第五节", "complete": true, "chars": 3560 }
  ],
  "targetedEvidence": [],
  "searchResults": [ ... ],
  "synonymExpansion": { ... }
}
```

`answer` 中的 `[1]`、`[2]` 编号对应 `documents` 中 V7 完整章节证据的顺序。同一章节命中的多个 chunk 会合并为一份证据。

未配置 `LLM_API_KEY` 时：

```json
{
  "llmConfigured": false,
  "answer": "未启用可用 LLM，已按当前选择的证据文档直接拼接如下。……",
  "prompt": "用户问题：...\n\n原文证据：...",
  "context": "...",
  "model": "gpt-4.1-mini"
}
```

## 领域知识层（匿名 GET，写入需管理员）

每个项目在 `backend/data/domain_layers/{project_hash}/` 下生成同义词文件：

```
synonym.yaml   # 同义词
```

### GET / PUT /api/projects/{project_id}/domain-layer/synonyms/

GET 返回：

```json
{
  "synonyms": { "登录": ["登陆", "认证入口"] },
  "yaml": "登录:\n- 登陆\n- 认证入口\n"
}
```

PUT 请求体（二选一）：

```json
{ "yaml": "登录:\n- 登陆\n" }
```

```json
{ "synonyms": { "登录": ["登陆"] } }
```

### POST /api/projects/{project_id}/domain-layer/synonyms/import/

导入同类词并合并到现有 synonym。

请求体：

```json
{
  "text": "撮合:\n- 竞价\n- 集合竞价\n\n接口: API, HTTP接口",
  "merge": true
}
```

返回合并后的 `synonyms`。

## 知识摄入（仅管理员）

未登录或非管理员调用会返回 403：

```json
{ "error": "需要管理员权限", "needLogin": true, "needAdmin": true }
```

### DELETE /api/projects/

移除本地项目登记（磁盘文件不删，仅管理员）。

```json
{ "id": "2886e610-7220-48ea-add6-f11c6d6a9fad" }
```

### POST /api/projects/register/

登记本地原文项目并建立 V7 文档、章节树和 FTS 索引，不调用 LLM。

请求体：

```json
{
  "projectDir": "D:\\KnowledgeProjects\\rules",
  "rawDir": "",
  "exportDir": "",
  "projectName": "法规知识库"
}
```

返回：

```json
{
  "ok": true,
  "registeredProject": { "id": "2886e610-...", "name": "法规知识库", "path": "..." },
  "projectDir": "...",
  "rawDir": "...",
  "exportDir": "...",
  "summary": {
    "sources": 12,
    "objects": 38,
    "hasIndex": true,
    "hasLog": true
  }
}
```

### POST /api/ingestion/build/

异步解析原文并直接写入 V7 内容库。子进程产生的一次性摄入载荷在原子索引提交后删除；任务记录保存在 Django 数据库。

历史任务通过 `GET /api/ingestion/jobs/?limit=100` 查询；完整日志通过 `GET /api/ingestion/jobs/<job_id>/` 查询。两个接口都仅限管理员使用。

不同项目的任务可以并行；相同项目或相同 `exportDir` 同时提交返回 HTTP 409。模型 HTTP 429 会产生 `rate_limited` 状态；`POST /api/ingestion/jobs/<job_id>/retry/` 创建新尝试并把并发数减半，最低为 1。

请求体：

```json
{
  "projectDir": "D:\\KnowledgeProjects\\rules",
  "rawDir": "",
  "exportDir": "",
  "limitDocs": 0,
  "llmConcurrency": 4,
  "maxItemsPerChunk": 10,
  "maxObjects": 160,
  "allowHeuristic": false,
  "projectName": "法规知识库"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `projectDir` | 是 | 本地项目根目录 |
| `rawDir` | 否 | 默认 `项目目录/raw/sources` |
| `exportDir` | 否 | 只读 Markdown 导出目录，默认 `项目目录/exports/wiki` |
| `limitDocs` | 否 | 仅处理前 N 个文档，0 = 全部 |
| `llmConcurrency` | 否 | 单任务并行调用 LLM 的标题块数量，默认读取 `LLM_CONCURRENCY` 或使用 4；多任务时总请求并发约为各任务并发数之和 |
| `maxItemsPerChunk` | 否 | 单切片保留的知识对象总数，最大且默认均为 10；提示模型优先提取 3–5 个 |
| `maxObjects` | 否 | 按来源覆盖度、出现次数和说明完整度全局排序后保留的知识对象总数，默认 160 |
| `allowHeuristic` | 否 | LLM 失败时是否降级为规则抽取 |
| `projectName` | 否 | 列表中显示名 |
| `timeoutSeconds` | 否 | 默认 7200；完整章节处理可能需要较长时间 |

返回任务对象：

```json
{
  "id": "abc123...",
  "status": "running",
  "ok": null,
  "startedAt": 1740000000.0,
  "finishedAt": null,
  "progress": { "current": 0, "total": 0, "percent": 0 },
  "projectDir": "...",
  "rawDir": "...",
  "exportDir": "...",
  "summary": {
    "sources": 0,
    "objects": 0,
    "hasIndex": false,
    "hasLog": false
  },
  "logs": [],
  "error": "",
  "command": [ ... ],
  "timeoutSeconds": 7200,
  "projectName": "法规知识库"
}
```

### GET /api/ingestion/jobs/{job_id}/

轮询生成任务进度。

```json
{
  "id": "abc123...",
  "status": "running",          // running / succeeded / failed / rate_limited
  "ok": true,
  "progress": { "current": 12, "total": 30, "percent": 40, "unit": "chunks" },
  "logs": [ "[块 12/30] xxx.docx · 竞价交易 > 订单确认 完成", "PROGRESS chunks 12 30", ... ],
  "summary": {
    "sources": 12,
    "concepts": 30,
    "entities": 8,
    "keywords": 5,
    "hasIndex": true,
    "hasLog": true
  },
  "finishedAt": null,
  "registeredProject": null
}
```

`status` 为 `succeeded` 时会带 `registeredProject`（自动登记到项目列表）。进度只解析机器标记 `PROGRESS chunks <current> <total>`；文档和块日志中的展示编号不会被误判为整体进度。

DOCX 按 Word 大纲/标题样式建树，保留段落、列表和表格文本语义，并仅在最深章节内切片。对象必须可独立解释；关系只接受 LLM 明确输出的带类型三元组。

### GET/POST /api/projects/{project_id}/search-index/

`GET` 校验本地项目索引并返回 V7 版本、DocumentVersion、StructuralNode、chunk、知识对象、词汇、Claim 和 ClaimEvidence 统计。管理员使用 `POST` 更新原文索引；该操作不调用 LLM，重新抽取知识应使用摄入任务。V6→V7 直接重建，不迁移旧知识数据。

### POST /api/projects/{project_id}/export/

管理员从当前 V7 内容库重建只读 Markdown 到项目 `exportDir`。导出会覆盖系统生成的 Markdown，并删除遗留 `structure.json`；不会修改原文或内容数据库。

## 调用示例

### curl

```bash
# 1. 登录并保存 cookie
curl -c cookies.txt -X POST http://127.0.0.1:8091/api/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-pass"}'

# 2. 检索问答（匿名也可，登录后能力不变）
curl -X POST "http://127.0.0.1:8091/api/projects/2886e610-7220-48ea-add6-f11c6d6a9fad/compose/" \
  -H "Content-Type: application/json" \
  -d '{"query":"认购邀请书的作用","limit":6}'

# 3. 管理员：启动知识摄入（需携带 cookie）
curl -b cookies.txt -X POST http://127.0.0.1:8091/api/ingestion/build/ \
  -H "Content-Type: application/json" \
  -d '{"projectDir":"D:\\LLMWikiProjects\\test","projectName":"测试库"}'

# 4. 轮询任务进度
curl -b cookies.txt "http://127.0.0.1:8091/api/ingestion/jobs/abc123.../"

# 5. 登出
curl -b cookies.txt -X POST http://127.0.0.1:8091/api/auth/logout/
```

### Python (requests)

```python
import requests

base = "http://127.0.0.1:8091"
s = requests.Session()

# 登录（管理员）
r = s.post(f"{base}/api/auth/login/", json={"username": "admin", "password": "your-pass"})
print(r.json())

# 检索问答
r = s.post(f"{base}/api/projects/2886e610-7220-48ea-add6-f11c6d6a9fad/compose/",
           json={"query": "认购邀请书的作用", "limit": 6})
data = r.json()
print(data["answer"])
print("证据文档:", [d["path"] for d in data.get("targetedEvidence", [])])

# 登出
s.post(f"{base}/api/auth/logout/")
```

## 错误返回格式

所有接口错误统一为 JSON：

```json
{ "error": "错误描述", "detail": "可选详情" }
```

常见状态码：

- `400` — 请求参数缺失或非法
- `401` — 登录失败
- `403` — 需要管理员权限
- `404` — 任务不存在
- `500` — 服务器内部异常
- `502` — 配置的 LLM 接口请求失败
