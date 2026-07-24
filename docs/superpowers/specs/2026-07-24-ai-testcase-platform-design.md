# AI 测试用例生成管理平台 — 设计规格

**日期：** 2026-07-24  
**状态：** 已审阅通过  
**阶段：** Demo / MVP  
**成功标准：** 金融/交易所场景下生成用例「看起来可用」（质量优先）

---

## 1. 背景与目标

### 1.1 问题

测试同学需要根据业务文档、接口说明与历史回归经验编写用例。纯手工成本高；纯「一键 LLM 生成」又缺少领域上下文与质量闭环，结果不稳定。

### 1.2 目标

搭建 Demo 级 **AI 测试用例生成管理平台**，核心闭环为：

```
文档入库 → LLM-Wiki 编译 → 需求驱动检索
  → AI 生成用例 → AI 评审 → 提示词迭代 → 终版确认
```

### 1.3 非目标（MVP 不做）

- Docker 独立知识中间件（如 WeKnora）
- 从 nashsu/llm_wiki 桌面端抽源码作后端
- 完整登录 / RBAC / 多租户
- 知识图谱、Deep Research、Chrome 剪藏
- 完整 Wiki 协作编辑器与版本 diff UI
- 重型向量集群 / 分布式队列
- 移动端适配与运营看板

### 1.4 关键决策摘要

| 决策点 | 结论 |
|--------|------|
| 成功标准 | 生成质量优先（金融/交易所场景） |
| 知识策略 | LLM-Wiki 为主 + 轻量检索（方案 C） |
| 知识实现 | 自研轻量层（β），FastAPI 内实现，无额外 Docker |
| 业务架构 | 质量优先流水线（方案 1） |
| 技术栈 | Vue 3 + TypeScript 前端；Python FastAPI 后端 |
| 模型 | 多模型，OpenAI 兼容协议可配置 |
| MVP 必做 | 文档上传+Wiki 编译检索；需求→生成→评审；提示词管理+迭代 |
| MVP 做薄（仍交付） | 任务步骤日志式过程可视化；Markdown 只读渲染；模型/Prompt/任务的基础管理页 |
| MVP 明确延后 | 登录/RBAC；完整用例资产库工作流；向量检索；Wiki 协作编辑 |

---

## 2. 系统架构与模块边界

### 2.1 一句话架构

单仓双端：**Vue3 工作台** + **FastAPI 业务与知识内核**；知识以本地 Markdown Wiki 沉淀；生成/评审走可配置的 OpenAI 兼容模型。

### 2.2 逻辑架构

```
┌──────────────────────────────────────────────────────────┐
│  Vue 3 + TypeScript 前端                                   │
│  文档库 | Wiki 浏览 | 提示词 | 生成工作台 | 任务详情        │
└────────────────────────────┬─────────────────────────────┘
                             │ REST（任务进度可轮询）
┌────────────────────────────▼─────────────────────────────┐
│  FastAPI 应用层                                            │
│  知识模块 | 生成闭环模块 | 配置模块 | LLM Gateway           │
│  SQLite（元数据） + 文件系统（raw/ + wiki/）                │
└──────────────────────────────────────────────────────────┘
```

### 2.3 模块职责

| 模块 | 职责 | 不负责 |
|------|------|--------|
| 知识模块（LLM-Wiki） | 原文入库、解析、编译 Wiki、index、检索与引用 | 不写测试用例、不做业务评审 |
| 生成闭环模块 | 需求、组上下文、生成/评审、Prompt 迭代、任务与结果 | 不直接管解析细节 |
| 配置模块 | 多模型配置、Prompt 模板 CRUD | 不跑流水线 |
| LLM Gateway | 统一 chat completions、超时与错误包装 | 不关心业务语义 |
| 前端 | 操作入口、Markdown 展示、步骤与引用展示 | 不直连外部模型 |

### 2.4 磁盘布局

```
data/
  raw/sources/           # 原始上传
  wiki/
    index.md             # 目录
    pages/               # 编译出的 Wiki 页
  meta/
    app.db               # SQLite
```

Wiki 页 frontmatter 最小约定：

```yaml
---
title: 现货下单接口规则
type: api_rule | business | regression | source_summary
sources: ["raw/sources/xxx.pdf"]
tags: ["交易", "下单"]
updated_at: 2026-07-24
---
```

### 2.5 部署形态

- 开发：`uvicorn` + `vite` 双进程  
- Demo：后端可托管前端静态资源；单机、无额外 Docker 依赖  
- 模型：HTTPS 调用外部/内网 OpenAI 兼容 API  

### 2.6 仓库结构

```
ai-testcase-platform/
  backend/
    app/
      api/
      services/
      models/
      default_prompts/
  frontend/
  data/                    # gitignore 运行时数据
  docs/superpowers/specs/
  README.md
```

---

## 3. 知识模块（上传 / 编译 / 检索）

### 3.1 目标

将业务文档、接口说明、回归用例编译为可检索的持久 Wiki，供生成/评审注入上下文。

### 3.2 对象

| 对象 | 说明 |
|------|------|
| SourceDocument | 上传原文元数据与状态 |
| WikiPage | 编译产物（文件 + 索引行） |
| IngestJob | 编译任务与步骤日志 |
| RetrieveHit | 检索命中（含 score/snippet） |

正文以 `wiki/pages/*.md` 为准；SQLite 存元数据与任务。

### 3.3 上传与解析

**MVP 格式：** `.md` / `.txt` 直接读入；`.pdf` 用 `pypdf` 抽文本；`.docx` 可选（`python-docx`）。

**流程：**

1. 上传 → `data/raw/sources/{uuid}_{filename}`  
2. SHA256；已成功编译且 hash 相同则默认跳过（可强制重编）  
3. 解析纯文本；过长则分段分析，编译仍以文档为粒度控制出页数  
4. 状态：`uploaded` → `parsed` → `ingesting` → `ready` / `failed`  

### 3.4 Wiki 编译（两步）

借鉴 Karpathy / llm_wiki 方法论，范围收窄为可维护的 Demo 实现：

**Step A 分析（LLM）**  
输入：原文（或分段）+ purpose（金融/交易所测试知识库）  
输出 JSON：`summary_title`、`key_rules[]`、`api_points[]`、`test_hints[]`、`entities[]`、`suggested_page_types`

**Step B 写入（LLM）**  
输入：Step A JSON + 现有 index 摘要  
输出：1~N 个 Markdown 页并落盘；至少 1 页 `source_summary`；更新 `wiki/index.md`；写 IngestJob 日志

**MVP 页类型：** `source_summary` | `business` | `api_rule` | `regression`

**编译 Prompt 类型：** `wiki_analyze` / `wiki_write`（可在提示词管理中修改）

**上限：** 单源生成页数设上限（如 8），防止膨胀。

### 3.5 检索（轻量）

`retrieve(query, top_k=5~8, types?)`：

1. 对 title、tags、正文关键词打分（中文子串/n-gram + 标题加权）  
2. 可按 `type` 过滤  
3. 在总字符预算内返回正文（如合计 ≤ 12k 字，可配置）  
4. 返回 path/title/type/score/snippet 供引用展示  

向量检索为二期可选；接口保持 `retrieve` 不变。

### 3.6 知识侧 API

```
POST   /api/documents
GET    /api/documents
GET    /api/documents/{id}
POST   /api/documents/{id}/ingest
GET    /api/ingest-jobs/{id}

GET    /api/wiki/pages
GET    /api/wiki/pages/{id}
GET    /api/wiki/index
POST   /api/wiki/retrieve
```

### 3.7 与生成模块契约

知识模块仅暴露：`retrieve`、`get_page`。  
生成模块默认不直接读 raw（MVP 不附带原文片段开关）。

---

## 4. 生成闭环（需求 → 生成 → 评审 → Prompt 迭代）

### 4.1 对象

| 对象 | 说明 |
|------|------|
| Requirement | 测试需求 |
| PromptTemplate | 提示词模板与版本 |
| GenerationTask | 闭环任务与状态机 |
| CaseDraft | 用例 Markdown 多版本 |
| ReviewResult | 评审结构化结果 |
| PromptRevision | 评审驱动的 Prompt 改写 |
| TaskEvent | 步骤日志（过程可视化） |

### 4.2 状态机

```
draft → retrieving → generating → generated
                         ↓
                     reviewing → reviewed
                         ↓
         optimizing | regenerating | finalized
任意步 → failed（可从失败步重试）
```

### 4.3 主流程

1. **创建需求/任务**：描述必填；可选关注点、模型、生成 Prompt；可勾选生成后自动评审  
2. **检索**：`query = 标题 + 描述 + 关注点` → 写入 `citations[]`  
3. **生成**：system=生成 Prompt；user=需求 + 编号 Wiki 上下文 + 输出骨架要求 → `CaseDraft` v1  
4. **评审**：输出优先 JSON（score/verdict/issues/missing_scenarios/prompt_improvement_hints/ready_for_final）  
5. **Prompt 迭代**：optimize 模板产出新 Prompt → 用户预览后「全局新版本」或「仅本任务临时」→ 再生成 v2（保留历史）  
6. **终版**：标记某版草稿 `finalized`；可复制/导出 Markdown  

### 4.4 用例输出骨架

```markdown
# 用例：xxx
- 优先级：P0/P1/P2
- 类型：功能/异常/边界/权限/资金安全
- 关联知识：[1][3]

## 前置条件
## 测试步骤
1. ...
## 预期结果
## 数据与环境备注（可选）
```

### 4.5 Prompt 管理

- 类型：`generate` | `review` | `optimize` | `wiki_analyze` | `wiki_write`  
- 字段：name、content、version、is_active  
- MVP：后端代码组装 user 消息；模板主要承载角色与质量规则  
- 启动时若无启用模板，写入内置中文默认模板（金融/交易所测试向）

### 4.6 模型配置

- name、base_url、api_key、model_name、is_default  
- 任务级可覆盖；生成与评审可选用不同模型  
- 提供连通性测试接口  

### 4.7 闭环侧 API

```
CRUD  /api/requirements
CRUD  /api/prompts
CRUD  /api/models
POST  /api/models/{id}/ping

POST  /api/tasks
GET   /api/tasks
GET   /api/tasks/{id}
POST  /api/tasks/{id}/generate
POST  /api/tasks/{id}/review
POST  /api/tasks/{id}/optimize-prompt
POST  /api/tasks/{id}/apply-prompt
POST  /api/tasks/{id}/regenerate
POST  /api/tasks/{id}/finalize
GET   /api/tasks/{id}/drafts
GET   /api/tasks/{id}/events
```

长耗时推荐：操作立即返回，前端轮询任务与 events。

### 4.8 质量默认策略

- 无检索命中：允许生成，前端强提示质量风险  
- `ready_for_final` 且 score ≥ 80（可配置）：高亮可终版  
- 所有 LLM 调用经 Gateway 记模型、耗时、成败  

---

## 5. 前端信息架构

### 5.1 布局

左侧导航 + 右侧内容。UI 库默认 **Element Plus**；Markdown 只读渲染。

### 5.2 路由

| 路由 | 页面 |
|------|------|
| `/` | 生成工作台 |
| `/tasks` | 任务列表 |
| `/tasks/:id` | 任务详情（闭环主舞台） |
| `/documents` | 文档库 |
| `/wiki` | Wiki 浏览 |
| `/prompts` | 提示词管理 |
| `/models` | 模型配置 |

### 5.3 关键交互

- **工作台：** 填需求 → 选模型/Prompt → 开始生成 → 跳转详情  
- **任务详情：** 时间线 + 多版本用例 Markdown + 引用 + 评审卡片 + 状态化操作条  
- **Prompt 优化：** 必须先看 diff，禁止静默覆盖启用 Prompt  
- **文档库：** 上传、编译、步骤日志、失败重试  
- **Wiki：** 搜索/类型筛选、只读预览  

### 5.4 交互原则

1. 主路径 ≤ 3 次点击见结果  
2. 质量信号外显（无命中、分数、可终版）  
3. 过程可解释（步骤日志）  
4. 中文优先 UI  

---

## 6. 数据模型

### 6.1 表（逻辑）

- **models** — 模型 endpoint 配置  
- **prompt_templates** — 提示词版本；同 type 至多一个 is_active  
- **documents** — 上传文档元数据  
- **ingest_jobs** — 编译任务与 step_log  
- **wiki_pages** — Wiki 索引（正文在文件）  
- **requirements** — 测试需求  
- **generation_tasks** — 闭环任务  
- **task_citations** — 任务级 Wiki 引用  
- **case_drafts** — 用例多版本  
- **review_results** — 评审结果  
- **prompt_revisions** — Prompt 改写记录  
- **task_events** — 过程事件  

### 6.2 配置项（环境变量示例）

```
APP_DATA_DIR=./data
LLM_DEFAULT_TIMEOUT_SEC=120
RETRIEVE_TOP_K=6
MAX_WIKI_CONTEXT_CHARS=12000
```

---

## 7. 错误处理

| 场景 | 处理 |
|------|------|
| LLM 超时/HTTP 错误/空响应 | 可读错误 + task_events + 任务/Job failed |
| 文档解析失败 | 文档级 failed，不影响其他文档 |
| 评审 JSON 解析失败 | 尝试代码块提取；再失败则 verdict=unknown，保留原文 |
| 检索为空 | 警告事件 + 前端横幅，非硬错误 |
| API 校验 | 统一 `{ code, message, detail? }`，422 |
| api_key | 列表接口掩码；不进前端打包配置 |

重试策略：从失败步继续；再生成追加 draft 版本，不默认清空历史。

---

## 8. 测试与验收

### 8.1 自动化（优先）

- 检索打分（中文标题命中）  
- 任务状态机合法/非法迁移  
- 文档 hash 去重与强制重编  
- Prompt is_active 唯一性  
- LLM Gateway 使用 httpx mock  

### 8.2 演示验收清单

1. 上传接口说明 + 业务规则 → 编译出 summary 与规则页  
2. 需求「现货限价单余额不足」→ 引用命中相关 Wiki  
3. 生成用例含步骤与预期，可见引用  
4. 评审给出 score 与 issues  
5. 优化 Prompt → diff → 再生成 v2，可与 v1 切换  
6. 终版标记成功  

### 8.3 暂缓

浏览器 E2E；真模型金标回归集。

---

## 9. 安全（Demo）

- 单机信任环境，无多用户隔离  
- 不执行用户文档中的代码  
- 上传大小限制（如 20MB）与扩展名白名单  
- api_key 仅存后端  

---

## 10. 方法论与外部参考（不嵌入依赖）

- **采用思想：** Karpathy LLM-Wiki（raw → 持久 wiki → 再查询/生成）  
- **参考实践：** nashsu/llm_wiki 的两步摄入、index.md、sources 追溯（**不抽取其 Tauri 桌面源码**）  
- **明确不用：** WeKnora 等重型独立中间件作为 Demo 依赖  

---

## 11. 后续迭代（非 MVP）

- Wiki 分类标签、人工精修、细粒度引用追溯  
- 可选向量检索  
- 登录与权限  
- 更强 PDF 解析  
- 用例库完整工作流与导出对接测试管理系统  

---

## 12. 设计审阅记录

- §1 架构与模块边界 — 用户确认  
- §2 知识模块 — 用户确认  
- §3 生成闭环 — 用户确认  
- §4 前端 — 用户确认  
- §5 数据/错误/测试 — 用户确认  
- 全文写入本文件 — 待用户审阅文件后进入实现计划  
