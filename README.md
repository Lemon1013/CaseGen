# CaseGen

AI 测试用例生成管理平台（Demo / MVP）。

文档上传 → LLM-Wiki 编译 → 需求驱动生成 → AI 评审 → 提示词迭代 → 终版用例。

目标场景：金融 / 交易所相关测试用例生成，**质量优先**。

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router |
| 后端 | Python FastAPI + SQLModel + httpx + pypdf |
| 存储 | SQLite（`data/meta/app.db`）+ 本地 Markdown Wiki（`data/wiki/`） |
| 模型 | 可配置的 OpenAI 兼容多模型（UI 配置 base_url / model / api_key） |

## 目录结构

```
CaseGen/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # REST 路由
│   │   ├── models/          # SQLModel 实体
│   │   ├── schemas/         # 请求/响应 schema
│   │   ├── services/        # Wiki / 任务流水线 / LLM Gateway
│   │   ├── default_prompts/ # 内置 Prompt 种子
│   │   ├── config.py
│   │   ├── db.py
│   │   └── main.py
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── frontend/                # Vue 3 前端
│   └── src/
│       ├── api/
│       ├── components/
│       ├── layouts/
│       ├── router/
│       └── views/
├── data/                    # 运行时数据（可覆盖 APP_DATA_DIR）
│   ├── raw/sources/         # 上传原文
│   ├── wiki/                # index.md + pages/
│   └── meta/                # app.db
├── fixtures/                # 演示样例文档
├── docs/superpowers/
│   ├── specs/               # 设计规格
│   └── plans/               # 实现计划
└── README.md
```

## 快速启动

### 1. 后端

在仓库根目录：

```bash
cd backend
python -m venv .venv

# Windows (Git Bash / PowerShell)
source .venv/Scripts/activate   # Git Bash
# .venv\Scripts\Activate.ps1    # PowerShell

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt

# 可选：复制环境变量示例并按需修改
# cp .env.example .env

uvicorn app.main:app --reload --port 8000
```

健康检查：`http://127.0.0.1:8000/api/health` → `{"status":"ok"}`。

API 文档：`http://127.0.0.1:8000/docs`。

### 2. 前端

另开终端：

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 Vite 提示的本地地址（默认 `http://127.0.0.1:5173`）。  
开发服务器已将 `/api` 代理到 `http://127.0.0.1:8000`。

### 3. 配置第一个模型

1. 打开前端 **模型管理** 页面  
2. 新增模型，填写：
   - **名称**：自定义显示名  
   - **base_url**：OpenAI 兼容接口根地址（如 `https://api.openai.com/v1` 或兼容网关）  
   - **model**：模型名  
   - **api_key**：密钥（仅存后端，不入库外泄到前端日志）  
3. 保存并设为可用 / 默认（按 UI 操作）

无真实密钥时，后端单元测试通过 httpx mock 覆盖 LLM 调用；演示闭环需要可访问的兼容接口。

### 4. 环境变量（可选）

见 [`backend/.env.example`](backend/.env.example)。关键项：

| 变量 | 含义 | 默认 |
|------|------|------|
| `APP_DATA_DIR` | 数据根目录 | 仓库根下 `data/` |
| `LLM_DEFAULT_TIMEOUT_SEC` | LLM 请求超时（秒） | `120` |
| `RETRIEVE_TOP_K` | Wiki 检索返回条数 | `6` |
| `MAX_WIKI_CONTEXT_CHARS` | 注入生成的 Wiki 上下文上限 | `12000` |
| `FINAL_SCORE_THRESHOLD` | 终版评分门槛 | `80` |

## 演示路径（验收清单）

与设计规格 §8.2 对齐的 Demo 闭环：

1. **上传样例文档**  
   在 **文档** 页上传仓库内样例：  
   [`fixtures/sample_balance_rules.md`](fixtures/sample_balance_rules.md)  
   （现货限价单余额不足应拒绝下单的业务/接口规则）

2. **编译 Wiki**  
   对文档触发编译 / 摄入，等待 job 成功。  
   在 **Wiki** 页确认出现 summary 与规则相关页面（如余额校验、错误码等）。

3. **工作台生成**  
   打开 **工作台**，创建任务，需求描述使用：  
   **「现货限价单余额不足」**  
   选择已配置模型，执行生成。  
   期望：用例含步骤与预期结果，并可见 Wiki 引用。

4. **评审 → 优化 Prompt → 再生成 → 终版**  
   - 对生成结果执行 **评审**，查看 score 与 issues  
   - 进入 **Prompt** 优化（或按 UI 触发优化），查看 diff  
   - **再生成** 得到 v2，可与 v1 切换对比  
   - 满足门槛后 **终版** 标记成功  

对应规格条目：上传编译 → 检索命中 → 生成带引用 → 评审 → Prompt 迭代 → 终版。

## 后端测试

```bash
cd backend
source .venv/Scripts/activate   # 或对应平台激活方式
pytest -v
```

## 设计与计划文档

- 设计规格：[`docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md`](docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md)
- 实现计划：[`docs/superpowers/plans/2026-07-24-ai-testcase-platform.md`](docs/superpowers/plans/2026-07-24-ai-testcase-platform.md)

## 仓库

远程：`git@github.com:Lemon1013/CaseGen.git`
