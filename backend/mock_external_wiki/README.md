# Mock 外部知识库服务（Mock External LLM Wiki）

用于端到端验证 CaseGen 主流程对外部 LLM Wiki 的调用。协议遵循
`docs/llm-wiki-reference/API_README.md` 中被
`app/services/external_wiki_client.py` 实际使用的两个端点：

1. `GET /api/projects/` — 列出外部项目
2. `POST /api/projects/{project_id}/search/` — 知识切片检索

实现只依赖 **Python 标准库**（`http.server`），零第三方依赖，不写盘、不访问外网。

## 启动

```bash
cd backend
python mock_external_wiki/server.py --port 8020 --host 127.0.0.1
```

- `--port 0` 会绑定随机空闲端口，并在启动行打印实际端口（便于测试）。
- `Ctrl+C` 优雅退出（`KeyboardInterrupt` → `server_close()`）。
- 每个请求会向 stdout 输出一行访问日志（时间、方法、路径、query、返回条数）。

启动输出示例：

```text
Mock external LLM Wiki listening on http://127.0.0.1:8020
Projects: proj-sse-rules (上交所交易规则知识库), proj-fund-rules (基金业务规则知识库)
Press Ctrl+C to stop.
```

## 端点

### GET /health

```json
{ "status": "ok" }
```

### GET /api/projects/

始终返回全部内置项目（与具体项目无关）：

```json
{
  "projects": [
    { "id": "proj-sse-rules", "name": "上交所交易规则知识库", "description": "..." },
    { "id": "proj-fund-rules", "name": "基金业务规则知识库", "description": "..." }
  ]
}
```

### POST /api/projects/{project_id}/search/

请求体：

```json
{ "query": "集合竞价 成交价", "limit": 8, "useSynonyms": true }
```

成功 200：

```json
{
  "results": [
    {
      "chunkId": 101,
      "documentPath": "交易规则/上海证券交易所交易规则(2023修订).docx",
      "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 集合竞价",
      "title": "集合竞价成交价格的确定",
      "score": 57.7,
      "snippet": "集合竞价时，成交价格的确定原则为：……",
      "evidenceSnippet": "第九十四条 集合竞价时，……",
      "highlightTerms": ["集合竞价", "最大成交量", "成交价格"]
    }
  ],
  "searchContextId": "mock-3f2a9c1b8d40",
  "elapsedMs": 1.2,
  "synonymExpansion": { "query": "...", "expandedTerms": ["..."] }
}
```

错误：

- 未知 `project_id` → `404 {"error": "project not found"}`
- 缺少/空 `query` → `400 {"error": "query is required"}`
- 非法 JSON → `400 {"error": "invalid JSON body"}`

### GET 其他路径

一律 `404 {"error": "not found"}`。

## 内置切片数据

| 项目 | 切片数 | 主题（title） |
| --- | --- | --- |
| `proj-sse-rules` 上交所交易规则知识库 | 8 | 集合竞价成交价格的确定、股票交易涨跌幅限制（10%）、大宗交易申报要素与数量门槛、撤销申报（撤单）的时间限制、开盘价与收盘价的确定、竞价交易时间安排、连续竞价成交价格的确定、市价申报与保护限价 |
| `proj-fund-rules` 基金业务规则知识库 | 7 | 申购费用与费率结构、赎回款项的支付时限、巨额赎回的认定与处理、基金资产估值方法、基金收益分配条件、ETF申购赎回清单与最小申赎单位、定期定额投资扣款规则 |

每个切片字段：`chunkId`（int）、`documentPath`、`headingPath`、`title`、`score`、
`snippet`、`evidenceSnippet`、`highlightTerms`。内容为手写的中文金融/交易所规则
演示数据，彼此可区分（例如搜“集合竞价”应命中“集合竞价成交价格的确定”，搜“巨额赎回”
应命中基金库的“巨额赎回的认定与处理”）。

### 评分与同义词（确定性，无随机性）

- 把 query 切成 CJK 2-gram/整词与拉丁词元，在 `title`（权重 3）、`headingPath`（2）、
  `snippet`（1）、`evidenceSnippet`（1）中做子串匹配计分，命中词元越长得分越高。
- `useSynonyms=true` 时按内置同义词表扩展词元（如 `竞价↔撮合`、`涨跌停↔涨跌幅限制`、
  `撤单↔撤销申报`），扩展词按 0.6 折算，体现该参数有实际效果；扩展命中的词同样进入
  `highlightTerms`。
- 切片的静态 `score` 仅作为基础权重小加成（×0.1）参与排序；切片数据中的
  `highlightTerms` 是该切片特征词，若同时出现在 query 与切片正文中则获得额外加成。
- 无任何命中的切片不返回（score 0 不出结果）；全部无命中时返回空 `results`。
- 结果按 score 降序、chunk 顺序稳定排序后截取 `limit`（默认 8，钳制在 1–50）。

## 配合 CaseGen 端到端验证

前置：mock 服务已启动（默认 `http://127.0.0.1:8020`）；CaseGen 后端运行在
`http://127.0.0.1:8000`。CaseGen 端所有 `/api` 请求需携带登录 cookie；写操作
（POST/PUT）还需带与后端同源的 `Origin` 头；项目维度接口需带 `project_id` query
参数。本地管理员账号：`admin` / `Casegen@2026`。

```bash
# 1. 启动 mock（另开终端）
python backend/mock_external_wiki/server.py --port 8020

# 2. 登录 CaseGen 并保存 cookie
curl -s -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"Casegen@2026"}'

# 3.（可选）创建一个演示项目，取回 project_id
curl -s -b cookies.txt -X POST http://127.0.0.1:8000/api/projects \
  -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8000" \
  -d '{"name":"外部知识库演示项目","slug":"mock-ext-demo"}'
PROJECT_ID=<上一步返回的 id>

# 4. 查看外部知识库配置（默认 base_url 为 http://127.0.0.1:8091）
curl -s -b cookies.txt \
  "http://127.0.0.1:8000/api/projects/${PROJECT_ID}/external-wiki?project_id=${PROJECT_ID}"

# 5. PUT 绑定 mock 服务（启用状态）
curl -s -b cookies.txt -X PUT \
  "http://127.0.0.1:8000/api/projects/${PROJECT_ID}/external-wiki?project_id=${PROJECT_ID}" \
  -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8000" \
  -d '{
    "name": "上交所交易规则知识库",
    "base_url": "http://127.0.0.1:8020",
    "external_project_id": "proj-sse-rules",
    "external_project_name": "上交所交易规则知识库",
    "top_k": 6,
    "timeout_sec": 5.0,
    "weight": 1.5,
    "use_synonyms": true,
    "enabled": true
  }'

# 6. 测试连接（应返回 success: true 与 2 个外部项目）
curl -s -b cookies.txt -X POST \
  "http://127.0.0.1:8000/api/projects/${PROJECT_ID}/external-wiki/test-connection?project_id=${PROJECT_ID}" \
  -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8000" \
  -d '{"base_url":"http://127.0.0.1:8020","timeout_sec":5.0}'

# 7. 混合检索验证：响应的 hits 中应出现 citation_type = "external"、
#    page_type = "external_wiki" 的外部命中；mock 窗口同步打印访问日志
curl -s -b cookies.txt -X POST \
  "http://127.0.0.1:8000/api/wiki/retrieve?project_id=${PROJECT_ID}" \
  -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8000" \
  -d '{"query":"集合竞价 成交价如何确定","project_id":'"${PROJECT_ID}"'}'
```

## 自动化测试

```bash
cd backend
pytest tests/test_mock_external_wiki.py -q
```

测试 fixture 会在随机端口线程中启动 mock server，用例结束后自动关闭。
