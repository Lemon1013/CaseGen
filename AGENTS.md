# Repository Guidelines

## 项目结构与模块划分

CaseGen 是 Vue 3 + FastAPI 的单仓应用。`backend/app/api/` 放 REST 路由，`services/` 放 Wiki 摄入、混合检索、LLM 和任务流水线，`models/` 与 `schemas/` 分别放 SQLModel 实体和接口结构，`default_prompts/` 保存内置提示词。后端测试位于 `backend/tests/`。前端代码在 `frontend/src/`，按 `views/`、`components/`、`api/`、`router/` 和 `layouts/` 划分；静态资源位于 `src/assets/` 或 `public/`。`fixtures/` 提供演示文档，`docs/superpowers/` 保存设计规格与实施计划。`data/` 是运行时上传文件、Markdown Wiki 和 SQLite 数据，不纳入版本控制。

## 构建、测试与开发命令

后端要求 Python 3.11+。在 `backend/` 中执行：

```bash
python -m venv .venv
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
pytest -q
```

可用 `pytest tests/test_retrieve.py -q` 运行单个测试文件。前端要求 Node.js 18+，在 `frontend/` 中执行：

```bash
npm install
npm run dev       # Vite 开发服务器，/api 代理到 8000 端口
npm run build     # vue-tsc 类型检查并生成生产构建
npm run preview   # 本地预览构建结果
```

## 编码风格与命名规范

Python 使用四空格缩进、类型注解和 `snake_case`；类使用 `PascalCase`。路由应保持精简，解析、检索和状态流转放入服务层。Vue 使用 `<script setup lang="ts">`、两空格缩进和无分号风格；组件文件采用 `PascalCase.vue`，变量与函数采用 `camelCase`。前后端 API 字段保持现有 `snake_case`。项目暂未配置 Ruff、Black、ESLint 或 Prettier，修改时应遵循相邻代码格式。

## 测试要求

后端使用 `pytest` 与 `pytest-asyncio`，测试文件命名为 `test_<功能>.py`，测试函数命名为 `test_<行为>`。新增路由或服务时，应覆盖正常路径、校验错误、失败重试和状态转换。使用 `tmp_app_data` 隔离 SQLite、Wiki 和上传目录，并模拟 `httpx`/LLM 调用；单元测试不得访问真实模型。前端尚无自动化测试脚本，提交前至少运行 `npm run build`。

## 提交与合并请求

提交历史主要采用 Conventional Commits，如 `docs:`、`chore:`；继续使用简短、祈使式标题，例如 `feat: add hybrid wiki retrieval`、`fix: handle ingest timeout`。每个提交只处理一个主题。合并请求应说明用户可见行为、关联 Issue 或计划任务、列出验证命令；UI 变更附截图，接口或配置变化附迁移说明。

## 配置与安全

从 `backend/.env.example` 创建本地 `.env`，不要提交密钥。模型 API Key 存于本地 SQLite，`data/` 备份可能包含敏感信息。不要提交 `.venv/`、`node_modules/`、`frontend/dist/`、数据库或客户上传文档。

## 主代理与子代理职责

- 主代理只负责需求澄清、分析、规划、方案与架构设计、任务拆分、风险判断、代码审核、验收和结果汇总，不直接实施代码修改。
- 任何需要创建、编辑或删除代码及相关配置、测试的任务，都必须由主代理委派给 `implementer`；即使改动很小，也不得由主代理直接实现。
- 主代理委派实现前，应向 `implementer` 提供明确的方案、任务边界、相关文件和验收标准。
- 后端调查优先委派给 `backend_explorer`，前端调查优先委派给 `frontend_explorer`；两个探索代理均保持只读。
- `implementer` 负责代码实现、测试补充和相关验证，并向主代理报告修改内容、验证结果和剩余风险。
- 实现完成后，主代理负责检查差异并可委派给 `reviewer` 做独立只读审查；审核发现的问题应再次交给 `implementer` 修复。
- 不要让多个具有写权限的代理同时修改相同文件。可以并行执行相互独立的只读调查、测试分析和审查任务。
- 主代理必须等待所需子代理完成、核对其结论并完成最终验收后再交付。
- 如果当前客户端无法启动所需子代理，主代理应说明阻塞，不得改为自行实施代码。
