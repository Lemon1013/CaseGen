# CaseGen Win10 离线部署包说明

本目录配套 `/CaseGen` 源码包，用于**不联网**的 Windows 10 机器离线部署。

## 目录结构

```
CaseGen/                     # 解压后的项目源码
├── backend/
│   ├── app/                 # FastAPI 后端（已内置前端页面托管）
│   └── requirements.txt     # 在线安装用（本机可联网时）
├── frontend/
│   └── dist/                # 前端生产构建（平台无关，无需 Node/npm）
└── deploy/win10/
    ├── install.bat          # ① 一键安装（离线 pip，不联网）
    ├── run.bat              # ② 一键启动（单进程：后端 + 前端页面）
    ├── requirements-offline.txt
    └── backend_wheels/      # 31 个 Windows 版依赖包（Python 3.11 / win_amd64，含 socksio）
```

## 部署步骤（Win10 目标机器）

### 前置要求

1. 安装 **Python 3.11**（官网 python.org 下载，安装时务必勾选
   **"Add python.exe to PATH"**）。
   > 本包附带的后端依赖（`backend_wheels/`）为 **cp311 / win_amd64** 版本，
   > 已针对 Python 3.11 预下载。若改用 3.12/3.13，需在可联网机器上重新下载 wheel
   > （命令见文末"更换 Python 版本"）。
2. 将整个压缩包解压到目标机器（路径不要含中文或空格更稳妥，例如 `D:\CaseGen`）。

### 第一步：安装依赖（只需一次）

双击 `deploy\win10\install.bat`

- 自动创建 `backend\.venv` 虚拟环境；
- 从随包的 `backend_wheels/` **离线**安装全部依赖，**全程不访问网络**。

### 第二步：启动服务

双击 `deploy\win10\run.bat`

- 后端与前端由**同一个进程**提供（uvicorn 8000 端口）；
- 浏览器打开 **http://127.0.0.1:8000** 即可使用。

## 说明与注意

- **前端无需 Node.js / npm**：页面使用 `frontend/dist` 生产构建，由后端直接托管，
  路由回退到 `index.html`（SPA 兼容）。
- **数据目录 `data/` 未包含**：首次启动会自动创建 `data/`（SQLite、Wiki、上传目录），
  需在「模型配置」页重新填写模型网关（base_url / model / api_key）后使用；
  上传文档后需重新「编译/摄入」到 Wiki。
- **登录认证（默认开启）**：本版内置账号认证。首次访问会进入初始化（Setup）页面创建管理员账号，之后用该账号登录；前端新增登录 / 初始化 / 用例列表页面。若不需要认证，在 `run.bat` 的启动命令前加环境变量 `CASEGEN_AUTH_ENABLED=false` 后重启。
- **停止服务**：在 run.bat 窗口按 `Ctrl+C` 或直接关闭窗口。
- **修改端口**：编辑 `run.bat`，把 `--port 8000` 改成其它端口。
- **更换 Python 版本**（如改用 3.12）：在可联网的 Windows 机器上重新下载对应版本 wheel：

  ```
  cd backend
  pip download -r requirements.txt --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all: -d ..\deploy\win10\backend_wheels
  ```

  并把 `requirements.txt` 中 `uvicorn[standard]` 改为 `uvicorn`（uvloop 不支持 Windows），
  同时清空 `backend_wheels\` 后再下载，避免新旧版本混用。

## DSH Agent 会话（line A，可选能力）

CaseGen 后端通过本地 **DeepSeek Harness（DSH）web-service** 提供「Agent 对话」能力。
浏览器永远不直接访问 DSH 端口，所有请求都由后端代理。

### 前置条件（目标机器需要额外安装）

1. 安装 DSH 本体，并启用其 **web-service / dsh-webapi** 插件；
2. 服务监听回环地址 `127.0.0.1:31800`（默认 `authEnabled=false`，即不需要令牌）；
3. 存活探测：浏览器或命令行访问 `http://127.0.0.1:31800/api/v1/presets`，
   返回 `{"ok":true,...}` 即表示服务可用；
4. 若目标机器**没有** DSH 运行时，把 `AGENT_ENABLED` 设为 `false`：
   `/api/agent/*` 全部返回 **410**，前端菜单软降级隐藏，其余功能不受影响。

### 两种配置途径（本启动命令没有 `--env-file`）

`run.bat` 的启动命令是 `python -m uvicorn app.main:app ...`，**不带 `--env-file`**，
所以 `backend/.env` 在这条路径下**不会被读取**。可选：

- **途径 A（推荐，随包生效）**：直接改 `run.bat` 中已有的
  `if not defined XXX set XXX=...` 语句块（位于启动命令之前）。
  `if not defined` 意味着系统环境变量优先级更高，不会被这里覆盖。
- **途径 B（系统环境变量）**：在 Windows「系统属性 → 环境变量」里为当前用户设置
  同名变量后重新打开 `run.bat`。适合运维统一配置、不希望改动脚本的场景。
- 如确需使用 `backend/.env`，可自行把启动命令改为
  `".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env`。

### 变量清单（与 `backend/app/config.py` 默认值一致）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DSH_BASE_URL` | `http://127.0.0.1:31800/api/v1` | DSH web-service API 根地址，仅回环 |
| `DSH_API_TOKEN` | 空 | DSH 开启鉴权时的 Bearer 令牌 |
| `AGENT_ENABLED` | `true` | `false` 时 `/api/agent/*` 返回 410 |
| `AGENT_MAX_CONCURRENT` | `4` | 全局并发轮次上限（信号量） |
| `AGENT_WORKSPACE_ROOT` | `~/.casegen/agent-workspace` | `~` 展开为用户目录，工作区不落在项目内；设为空则回退 `<APP_DATA_DIR>/agent-workspace` |
| `AGENT_FIRST_TOKEN_TIMEOUT_SEC` | `600` | 首字读超时；模型冷加载可能 5–8 分钟 |
| `AGENT_STALL_TIMEOUT_SEC` | `300` | 无任何帧的停滞看门狗时长 |
| `AGENT_STAGE_MAX_CALLS` | `4` | 仅 line B 流水线预算，当前未使用 |
| `PIPELINE_AGENT_STAGES` | 空 | 空表示全部阶段仍走旧路径 |
| `AGENT_SKILLS_DIR` | `.agents/skills` | 技能目录（相对仓库根） |

### 工作目录与安全边界

- 每个对话一个独立工作目录 `<AGENT_WORKSPACE_ROOT>/<conversation_id>/`（默认
  `~/.casegen/agent-workspace/<conversation_id>/`，已移出 `data/`）；与保存明文
  模型 API Key 的 `<data>/meta/` 完全隔离，数据库文件永远不会成为 Agent 的工作目录；
- 改根目录只对**新会话**生效：老会话 `workspace_dir` 已落库，删除时守卫会跳过旧
  路径（可能留下孤儿目录，需手工清理）；Windows 用户名含中文或空格时建议改成简单
  绝对路径（如 `D:\CaseGenAgentWorkspaces`）；
- 会话创建后后端会立刻把 DSH 会话权限压降为 `workspace-write`；
  压降失败则整次创建失败（绝不带 `danger-full-access` 继续）；
- Agent 的思考过程、工具调用与工具结果**不落库、不转发、不写日志**，仅计数；
  转发给前端的错误文案是白名单原语，不含 DSH 原始响应体、URL 或凭据。

## 本机验证记录

- 后端 pytest：`350 passed`（认证、Wiki spaces、任务流式输出、测试设计工作台、检索确认点、停止生成、需求导入、外部知识库 mock）
- 前端 `npm run build`：通过
- 单进程模式（后端托管 dist）：健康检查、首页、SPA 路由、静态资源、API 全部 200
- Python 3.11.14 实机验证通过（后端 `backend/.venv311` 与 Win10 同步）
- 版本基线：`073a907`（Wiki 弹性 + Wiki spaces + 账号认证 + 任务流式输出 + 测试设计工作台 + 生成停止/进度 + 需求文档导入 + 外部知识库 mock）
- wheel 缓存：31 个 cp311 / win_amd64（含 `socksio`，配合 `httpx[socks]` 代理支持）

## 可选：外部知识库 mock 服务

验证"外部知识库"主流程时可启动随包的 mock（无需联网）：

```
cd backend
python mock_external_wiki\server.py --port 8020
```

内置 2 个演示项目（上交所规则 / 基金规则）。在 CaseGen 界面把项目的外部知识库
base_url 指向 `http://127.0.0.1:8020`（external_project_id 填 `proj-sse-rules`）
即可在检索与生成确认中看到外部命中。详见 `backend/mock_external_wiki/README.md`。
