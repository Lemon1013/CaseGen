# CaseGen Wiki 2.0 核心设计规格

**日期：** 2026-08-02  
**状态：** 待评审  
**参考：** [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)、Karpathy LLM Wiki 方法论  
**边界：** 借鉴知识编译理念并独立实现，不复制其桌面端、图谱、Agent 或 GPLv3 源码。

## 1. 背景与结论

CaseGen 当前已实现“原文 → 两步 LLM 分析/写页 → Markdown Wiki → 混合检索”，但页面仍以单份文档为中心：重新编译只替换该文档生成的页面，新文档不会稳定地更新已有规则、实体和场景页，也不会记录跨来源冲突。当前 Wiki 因而更接近摘要集合，而不是可持续演化的测试知识库。

Wiki 2.0 将核心定义为：**原文不可变，LLM 按领域规则把新知识增量整合进持久 Wiki；人负责来源、方向和高风险判断，LLM 负责归档、关联、更新与维护。**

## 2. 目标与非目标

### 2.1 目标

1. 新来源既生成来源摘要，也能创建、更新或合并跨来源知识页。
2. 规则、数值和结论可追溯到文档、条款及原文块。
3. 摄入前检索相关现有页面，识别重复、补充、冲突和过时知识。
4. 高风险更新进入审核；所有写入可查看 Diff、历史和回滚。
5. 维护 `purpose.md`、`schema.md`、`index.md`、`overview.md` 和 `log.md`。
6. 提供 Lint，发现孤立页、死链、无来源结论、冲突和陈旧页面。
7. 保留现有 SourceChunk 作为无损证据层，生成用例时以原文证据兜底。

### 2.2 非目标

- 不实现桌面端、Obsidian 配置、网页剪藏、多媒体和 Deep Research。
- 不在首期实现知识图谱可视化、社区发现或复杂 Agent。
- Embedding 是后续可选增强；首期使用全文检索、链接扩展和原文核验。
- 不把 Wiki 做成多人实时协作编辑器。

## 3. 三层架构

```text
Raw Sources（不可变事实层）
  文档、解析文本、页码/条款锚点、SourceChunk
                ↓ ingest
Wiki（可演化知识层）
  来源摘要、规则、实体、场景、回归风险、跨来源综合
                ↓ governed by
Purpose + Schema（方向与规则层）
  知识库目标、页面模型、引用约定、更新/冲突/审核策略
```

查询顺序为：Wiki 定位知识 → 关联扩展 → 回到 SourceChunk 核验 → 注入生成上下文。原文与 Wiki 冲突时，以可追溯原文为准并创建审核项。

## 4. 文件布局

```text
data/wiki/
  purpose.md
  schema.md
  index.md
  overview.md
  log.md
  sources/       # 每份来源固定一页
  rules/         # 条件→行为→结果的业务/资金/风控规则
  entities/      # 账户、订单、余额、产品、市场等
  scenarios/     # 下单、撤单、撮合、清算、异常恢复等
  regressions/   # 历史缺陷、易错点、回归风险
  synthesis/     # 跨来源对比、冲突和综合结论
```

`purpose.md` 固定描述“金融/交易所测试用例生成、质量与可追溯优先”；`schema.md` 定义本规格中的页面类型、字段、来源格式和合并规则。两者在每次摄入和查询时作为系统上下文，普通用户不能被 LLM 静默改写。

## 5. 页面模型

每页使用稳定 `page_key`，文件名变化不改变身份。最小 frontmatter：

```yaml
---
page_key: rule.order.insufficient-balance
title: 余额不足时的下单处理
type: rule
domain: spot-order
aliases: [可用余额不足, 余额不足下单]
tags: [余额, 下单, 资金安全]
sources:
  - document_id: 12
    chunk_ids: [81, 82]
    clauses: ["3.5.2"]
status: published
revision: 3
updated_at: 2026-08-02
---
```

正文优先表达：适用条件、规则、资金影响、异常/边界、测试提示、相关页面和来源。页面之间使用 `[[page_key|显示标题]]`。关键数值、时序、错误码和资金结论必须带来源锚点；无法确认的内容标记为“待审核”，不得伪装为确定规则。

## 6. 数据模型

现有 `WikiPageRow.source_document_id` 只能表达单来源，需平滑迁移为：

- `wiki_pages`：增加 `page_key`、`domain`、`status`、`revision`、`aliases_json`、`content_hash`。
- `wiki_page_sources`：页面与文档多对多，保存 `chunk_ids_json`、`clauses_json`。
- `wiki_page_revisions`：保存每次变更前后的正文、frontmatter、操作、Job 和时间。
- `wiki_review_items`：保存冲突、数值变化、合并建议及审核状态。
- `ingest_jobs`：增加阶段、进度、模型/Prompt 版本、计划 JSON 和取消标记。

迁移期间保留 `source_document_id` 只读兼容；旧 `pages/*.md` 首次重建时转换为 `sources/legacy-*`，不得猜测跨文档合并关系。

## 7. 增量摄入流程

### 7.1 生命周期

```text
queued → parsing → chunking → analyzing → planning
       → awaiting_review | applying → indexing → linting
       → ready | failed | cancelled
```

`POST /documents/{id}/ingest` 必须立即返回 Job；后台串行处理同一项目，禁止同一文档并发摄入。SHA256 未变化且 schema/Prompt 版本未变化时默认跳过，可强制重编。

### 7.2 分析前准备

1. 解析文档并校验空文本、乱码和提取质量。
2. 建立带页码、章节、条款号和字符范围的 SourceChunk。
3. 根据文件名、标题、实体、条款和摘要检索候选 Wiki 页面。
4. 只把相关页面、`purpose.md`、`schema.md` 和分类索引传给分析模型，不再只读取 `index.md` 开头。

### 7.3 Step A：知识分析与变更计划

长文仍按窗口覆盖，但每窗结果持久化，最终按条款和实体归并。输出采用严格 JSON：

```json
{
  "source_summary": {},
  "claims": [],
  "entities": [],
  "related_pages": [],
  "contradictions": [],
  "page_operations": [
    {
      "op": "create|update|merge|noop",
      "page_key": "rule.order.insufficient-balance",
      "reason": "补充冻结余额处理",
      "source_anchors": []
    }
  ],
  "review_items": []
}
```

计划必须通过后端校验：操作白名单、page_key 格式、目标页存在性、来源锚点有效性、页面类型和最大变更数量。模型不得直接指定任意文件路径。

### 7.4 Step B：生成候选变更

- `create`：生成符合 schema 的新页面。
- `update`：基于旧正文生成完整新版本，并附结构化 Diff 摘要。
- `merge`：仅用于重复页面，保留全部来源、别名和重定向关系。
- `noop`：来源已覆盖，不重复写页。

来源摘要始终创建或更新；普通新增页可自动应用。以下情况必须进入审核：已有规则被删除、关键数值变化、来源冲突、页面合并、低置信度或无有效锚点。

### 7.5 原子应用

先在 staging 目录写候选文件并完成校验，再在单次数据库事务中写页面、来源关系和修订记录，最后替换正式文件。失败时保留旧 Wiki，不允许出现“新原文块 + 部分新页面 + 旧索引”的混合状态。

## 8. 全局文件与维护

- `index.md`：按 domain/type 分类，包含链接、一句话说明和来源数，不是简单平铺。
- `overview.md`：概括当前业务域、关键规则、冲突和知识缺口；增量更新，必要时全量重建。
- `log.md`：追加摄入、审核、回滚和 Lint 记录，格式可解析。
- Lint：检查重复 page_key、无效 wikilink、孤立页面、无来源关键结论、冲突未处理、来源文件缺失、索引不一致和陈旧页面。

Lint 只报告问题；自动修复必须生成候选 Diff，不得静默改写知识。

## 9. 查询与生成上下文

1. 使用 SQLite FTS5/BM25 检索 Wiki 和 SourceChunk，标题、别名、条款号与标签加权。
2. 以高分 Wiki 页为种子，通过 `[[wikilink]]` 和共享来源扩展一跳。
3. 围绕真实命中位置生成 snippet，不再固定截取正文开头。
4. 上下文预算按“Wiki 解释 60% / 原文证据 35% / index 5%”分配，并确保每个命中页有公平上限。
5. 条款强锚定仅在查询明确包含条款号时启用；模型从 Wiki 推断的条款需再次通过关键词相关性校验。
6. 向量检索后续以可插拔方式加入，与 FTS 结果融合，不改变 API 契约。

## 10. API 与界面

核心新增接口：

```text
GET    /api/wiki/config                 # purpose/schema
POST   /api/documents/{id}/ingest       # 立即返回 queued Job
GET    /api/ingest-jobs/{id}            # 阶段、进度、步骤
POST   /api/ingest-jobs/{id}/cancel
GET    /api/wiki/reviews
POST   /api/wiki/reviews/{id}/approve
POST   /api/wiki/reviews/{id}/reject
GET    /api/wiki/pages/{id}/revisions
GET    /api/wiki/pages/{id}/diff
POST   /api/wiki/pages/{id}/rollback
POST   /api/wiki/lint
```

文档页展示解析预览、摄入进度和失败阶段；Wiki 页按类型/领域分组，展示来源、关联页、版本和 Diff；Review 页集中处理冲突与高风险变更。

## 11. 测试与质量门槛

- 单元测试：page_key、frontmatter、计划校验、合并规则、链接解析、来源锚点、命中位置 snippet。
- 集成测试：新建、更新、noop、冲突审核、回滚、失败原子性、重复 SHA 跳过、重启恢复。
- 检索评测：维护至少 30 条金融测试查询及期望页面/条款，记录 Recall@5、MRR、原文锚定正确率。
- 摄入验收：第二份相关文档应更新已有规则页而非产生重复页；删除/回滚不能留下死链。

首期目标：Recall@5 ≥ 85%，关键规则来源锚定率 100%，重复主题页率 < 5%，摄入接口 500ms 内返回 Job。

## 12. 分阶段交付

1. **基础治理：** purpose/schema、页面模型、修订与来源关系、旧数据迁移。
2. **增量摄入：** 相关页检索、变更计划、create/update/noop、异步队列和原子应用。
3. **审核维护：** merge/冲突 Review、Diff、回滚、index/overview/log、Lint。
4. **检索升级：** FTS5、链接扩展、命中摘要、评测集；Embedding 保持可选。

## 13. 设计原则

- 原文是事实层，Wiki 是可解释的编译产物。
- 先定位现有知识，再决定创建页面。
- 更新必须保留来源和历史；冲突不能被覆盖掉。
- 高风险判断交给人，机械维护交给 LLM。
- 先建立可评测的全文检索基线，再引入更复杂基础设施。

