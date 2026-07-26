# CaseGen

AI 测试用例生成管理平台（Demo / MVP）。

**文档上传 → LLM-Wiki 编译 → 需求驱动生成 → AI 评审 → 提示词迭代 → 终版用例。**

目标场景：金融 / 交易所相关测试用例生成，**质量优先**。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router |
| 后端 | Python FastAPI + SQLModel + httpx + pypdf + python-docx |
| 存储 | SQLite（`data/meta/app.db`）+ 本地 Markdown Wiki（`data/wiki/`） |
| 模型 | 可配置的 OpenAI 兼容多模型（UI 配置 base_url / model / api_key） |

---

## 环境要求（新机器必装）

在安装项目依赖之前，请先准备：

| 软件 | 版本要求 | 说明 |
|------|----------|------|
| **Python** | **3.11 或更高**（推荐 3.11 / 3.12 / 3.13） | 需能执行 `python -m venv`、`pip` |
| **Node.js** | **18 或更高**（推荐 20 LTS / 22+） | 需自带 **npm** |
| **Git** | 任意近期版本 | 用于克隆仓库（也可用 zip 解压源码） |
| 网络 | 可访问 LLM 网关 | 编译 Wiki / 生成用例需要 OpenAI 兼容 API |

可选自检：

```bash
python --version    # 应 >= 3.11
node --version      # 应 >= 18
npm --version
git --version
```

> Windows 建议使用 **Git Bash** 或 PowerShell；下文命令以跨平台 bash 风格为主，并注明 Windows 差异。

---

## 获取代码

```bash
git clone git@github.com:Lemon1013/CaseGen.git
cd CaseGen

# 若功能在功能分支上（例如 feat/mvp-platform）：
git checkout feat/mvp-platform
git pull
```

也可用 HTTPS：

```bash
git clone https://github.com/Lemon1013/CaseGen.git
cd CaseGen
```

---

## 一键安装依赖（推荐按顺序）

### 1. 后端 Python 依赖

在**仓库根目录**执行：

```bash
cd backend

# 创建虚拟环境（只需做一次）
python -m venv .venv

# 激活虚拟环境
# Windows Git Bash:
source .venv/Scripts/activate
# Windows PowerShell:
#   .\.venv\Scripts\Activate.ps1
# macOS / Linux:
#   source .venv/bin/activate

# 升级 pip（推荐）并安装全部后端依赖
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 会安装：

- 运行：`fastapi`、`uvicorn[standard]`、`sqlmodel`、`httpx`、`pypdf`、`python-docx`、`python-multipart`、`pyyaml`
- 测试：`pytest`、`pytest-asyncio`

验证：

```bash
python -c "import fastapi, sqlmodel, httpx, pypdf; print('backend deps ok')"
```

### 2. 前端 Node 依赖

另开终端（或退出 venv 后），在**仓库根目录**：

```bash
cd frontend
npm install
```

会按 `package.json` / `package-lock.json` 安装 Vue 3、Element Plus、Vite、markdown-it 等。

验证：

```bash
npx vite --version
```

### 3. 环境变量（可选）

```bash
cd backend
cp .env.example .env    # Windows 可用 copy .env.example .env
```

一般**可不改**即可启动。常用项：

| 变量 | 含义 | 默认 |
|------|------|------|
| `APP_DATA_DIR` | 数据根目录（库、Wiki、上传原文） | 仓库根下 `data/` |
| `LLM_DEFAULT_TIMEOUT_SEC` | 普通 LLM 超时（秒） | `180` |
| `LLM_WIKI_TIMEOUT_SEC` | Wiki 编译相关超时（秒） | `300` |
| `RETRIEVE_TOP_K` | 检索条数相关 | `6` |
| `MAX_WIKI_CONTEXT_CHARS` | 生成时 Wiki 上下文上限 | `12000` |
| `FINAL_SCORE_THRESHOLD` | 终版评分门槛 | `80` |
| `WIKI_ANALYZE_SINGLE_PASS_CHARS` | 长文单次分析字符上限 | `48000` |

> **不要**把 API Key 写进 `.env` 提交进 Git。模型密钥在前端 **模型配置** 页写入，保存在本地 SQLite。

---

## 启动服务

需要**两个进程**同时运行。

### 终端 A — 后端（默认端口 8000）

```bash
cd backend
source .venv/Scripts/activate   # 按你的系统改激活方式
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- 健康检查：<http://127.0.0.1:8000/api/health> → `{"status":"ok"}`
- Swagger：<http://127.0.0.1:8000/docs>

首次启动会自动创建 `data/meta/`、`data/wiki/`、`data/raw/sources/` 并初始化 SQLite。

### 终端 B — 前端（默认端口 5173）

```bash
cd frontend
npm run dev
```

浏览器打开 Vite 提示的地址（通常 <http://127.0.0.1:5173>）。  
开发服务器已将 **`/api` 代理到 `http://127.0.0.1:8000`**，前端无需再配跨域。

---

## 首次使用配置

1. 打开前端 **模型配置**
2. 新增模型并填写：
   - **名称**：显示名（如 deepseek）
   - **base_url**：OpenAI 兼容根地址（如 `https://api.deepseek.com` 或 `https://api.openai.com/v1`）
   - **model**：模型名（如 `deepseek-v4-pro`）
   - **api_key**：密钥
3. 设为**默认**模型
4. 在 **文档管理** 上传规则文档（支持 `.md` / `.txt` / `.pdf` / `.docx`），触发 **编译 / 摄入**
5. 在 **Wiki** 确认页面生成成功
6. 在 **工作台** 填写需求并生成用例

演示样例文档：[`fixtures/sample_balance_rules.md`](fixtures/sample_balance_rules.md)

---

## 目录结构

```
CaseGen/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # REST 路由
│   │   ├── models/          # SQLModel 实体
│   │   ├── schemas/         # 请求/响应
│   │   ├── services/        # Wiki / 检索 / 任务流水线 / LLM
│   │   ├── default_prompts/ # 内置 Prompt
│   │   ├── config.py
│   │   ├── db.py
│   │   └── main.py
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── frontend/                # Vue 3 前端
│   ├── src/
│   ├── package.json
│   └── package-lock.json
├── data/                    # 运行时数据（Git 忽略，启动后自动创建）
│   ├── raw/sources/         # 上传原文
│   ├── wiki/                # index.md + pages/
│   └── meta/                # app.db
├── fixtures/                # 演示样例
├── docs/superpowers/        # 设计规格与实现计划
└── README.md                # 本文件
```

---

## 后端测试

```bash
cd backend
source .venv/Scripts/activate
pytest -q
```

---

## 迁移到新机器时注意

| 要带 | 不要带 / 到新机重装 |
|------|---------------------|
| Git 源码（或完整 zip） | `backend/.venv/` |
| 可选：`data/`（保留 Wiki、任务、**含 API Key 的库**） | `frontend/node_modules/` |
| 可选：`backend/.env` | `__pycache__`、`.pytest_cache` |
| LLM 的 base_url / api_key（建议重新在 UI 配置） | 本机绝对路径脚本配置 |

`data/` 已在 `.gitignore` 中，**默认不会进 Git**。若要在新机延续现有 Wiki，请单独拷贝 `data/` 目录，并注意其中数据库含密钥。

---

## 常见问题

**Q: `python` 不是 3.11+？**  
安装官方 Python 3.11+，或使用 `py -3.11 -m venv .venv`（Windows）。

**Q: 前端能开但接口全失败？**  
确认后端已在 8000 端口运行；打开 <http://127.0.0.1:8000/api/health>。

**Q: 编译 Wiki / 生成一直失败？**  
检查模型 base_url、model、api_key 是否可从新机器访问对应网关；适当增大超时环境变量。

**Q: Wiki Index 点链接变空白页？**  
请使用本仓库最新代码：Index 链接为 `/wiki?page={id}`，前端会在页内打开，不会跳到无效的 `/pages/*.md`。

**Q: 只要生产静态前端？**  

```bash
cd frontend
npm run build
# 产物在 frontend/dist，需自行用 nginx 等托管，并把 /api 反代到后端 8000
```

当前 README 以**开发模式双进程**为主，便于本机与内网 Demo。

---

## 设计文档

- 设计规格：[`docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md`](docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md)
- 实现计划：[`docs/superpowers/plans/2026-07-24-ai-testcase-platform.md`](docs/superpowers/plans/2026-07-24-ai-testcase-platform.md)
- 长文 Wiki 分析：[`docs/superpowers/specs/2026-07-25-wiki-long-source-analyze-design.md`](docs/superpowers/specs/2026-07-25-wiki-long-source-analyze-design.md)

## 仓库

- 远程：`git@github.com:Lemon1013/CaseGen.git`
- HTTPS：`https://github.com/Lemon1013/CaseGen.git`
