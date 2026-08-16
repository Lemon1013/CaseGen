# 测试设计工作台设计与实施计划

## 目标与非目标

工作台持久化生成粒度（compact/standard/detailed）、通用测试维度、参考用例快照和可编辑的需求优化结果。检索确认后先生成结构化测试点，测试点经版本化人工确认后才生成完整用例；最终用例保存 P0/P1/P2、测试点关联和引用追踪，并以关系数据计算覆盖摘要。

非目标：目标平台配置、领域策略包、团队策略模板、测试集管理、第三方/多格式导出、分享评论协作、思维导图。

## 用户流程

1. 在工作台填写/选择需求，选择粒度、通用维度、模型、Prompt 和可选参考用例。AI 需求优化只返回可编辑建议与待确认问题，提交时才写入 Requirement。
2. 任务创建时校验参考用例为当前非归档用例，并保存标题、正文、来源和 SHA-256 快照；后续用例修改不影响任务。
3. 任务经历 `retrieving → awaiting_confirmation`。用户确认引用后进入 `generating_test_points → awaiting_test_point_confirmation`。
4. 用户可全量编辑、增加、删除、修改维度/优先级、取消选择或排除测试点；确认要求版本号、幂等键和至少一个有效选中点。
5. 确认后进入 `generating`，生成 Markdown 草稿时必须携带 TP 稳定 key 和 priority；终版导入把 priority 和测试点关系写入规范化表。测试点 JSON 在有限结构化修复重试后仍失败时，创建基于需求和已确认引用的确定性可编辑 fallback 检查点，任务停在测试点确认，不重新检索。

## 数据模型和状态机

`GenerationTask` 增加 `generation_granularity` 和 `test_dimensions_json`。`TaskReferenceCase` 保存不可变参考快照。`TaskTestPointCheckpoint` 保存测试点版本、决策 hash、幂等键和恢复租约；`TestPoint` 保存稳定 key、标题、验证目标、维度、优先级、排序、选择/排除；`TestPointCitation`、`DraftTestPointLink`、`TestPointCaseLink` 分别保存 citation→point、draft section→point、point→最终 TestCase 关系。`TestCase.priority` 为结构化优先级。

公共检索状态 `awaiting_confirmation` 只表示检索确认。新增 `generating_test_points` 和 `awaiting_test_point_confirmation`，后台 job claim/lease、SSE 和启动恢复同时支持 retrieval/test-point 两类检查点。旧任务、旧 Markdown 和旧用例缺失新字段时使用 standard、positive/negative/boundary、P1 等兼容默认值。

## API 契约

- `POST /api/tasks` 接受 `generation_granularity`、`test_dimensions`、`reference_case_ids`、`reference_text`，旧字段仍有效。
- `POST /api/tasks/requirement-optimize` 返回 `{title, description, questions}`；独立 `requirement_optimize` Prompt 不改变已有 `optimize`（Prompt 优化）含义。
- `GET/POST /api/tasks/{id}/retrieval-checkpoint` 保持现有确认契约；确认后只进入测试点阶段。
- `GET /api/tasks/{id}/test-points`、`PUT /api/tasks/{id}/test-points`、`POST /api/tasks/{id}/test-points/confirm` 提供查询、全量编辑和确认。
- `GET /api/tasks/{id}/references` 返回任务快照；`GET /api/tasks/{id}/coverage` 返回关系确定性计算的点覆盖、引用使用和未覆盖明细。
- Cases API 增加 `priority` 查询/输出/更新字段；旧 Markdown 导入仍可用。

## Prompt/结构化输出约束

新增 `test_points` 和 `requirement_optimize` 默认模板，沿用现有 seed/version 逻辑，不覆盖自定义 active Prompt。测试点模型输出 JSON，包含 stable_key、title、verification_goal、dimension、priority 和 citation_ids。服务端只接受当前任务 citation；本次上下文显式提供模型可见 label 到数据库 ID 的映射，未知 citation 丢弃并写诊断事件。参考用例作为独立结构化 JSON 用户消息序列化，带 `kind=reference_case_snapshots`、`trust=untrusted_style_only` 和来源/hash 元数据；只允许参考格式、拆分粒度和表达风格，事实只能来自需求、Wiki 和原文，不依赖可闭合标签边界，也不执行参考内容中的指令。

## 覆盖计算

选中且未排除的测试点为分母；存在 `TestPointCaseLink` 即视为覆盖。覆盖率、未覆盖点、每个 citation 使用的测试点和用例均由关系表确定性计算，模型不能自报百分比；无测试点时覆盖率为 0。若任务存在测试点检查点，终版严格要求每个 case section 都有当前 TP key 和可解析的 P0/P1/P2；只有完全没有测试点检查点的旧任务允许 legacy Markdown fallback（默认 P1，不能建立新的测试点覆盖关系）。

## 前端信息架构

Workbench 保留深色侧栏、蓝紫主题和 Element Plus，按需求优化、生成策略、参考用例页签、模型 Prompt 和提交摘要组织。TaskDetail 保留实时生成/评审/再生成/终版，并增加检索确认后的测试点确认阶段、配置摘要和覆盖 API 展示。CasesView 增加 P0/P1/P2 筛选与展示。

## 迁移、兼容、测试与风险

SQLite 启动时在 `db.py` 为旧 `generation_tasks`、`test_cases` 添加列；新规范化表由 `create_all` 创建并补索引，迁移幂等。新库模型声明 `retrieval_checkpoint_id` 与 `test_case_id` 外键并使用级联策略；旧 SQLite 表不会被无风险地原地重建补 FK，因此应用删除路径仍显式清理关系。后端应使用 mock LLM 覆盖：默认值、快照隔离、未知 citation、fallback、版本冲突、重复确认、恢复租约、priority/关系导入、严格/legacy 终版和确定性覆盖；不访问真实模型。前端以 `npm run build` 做类型与生产构建验证。

剩余风险包括：旧客户端/旧测试仍假定检索确认后直接生成，需要迁移到新的测试点确认契约；SSE 仍是进程内预览，跨进程部署应继续以持久化状态轮询为权威；旧生成 Markdown 没有 TP 标记时只能保持兼容导入而无法 retroactively 建立测试点覆盖关系。
