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
    ├── package.ps1          # 在联网构建机生成离线 ZIP
    ├── requirements-offline.txt
    └── backend_wheels/      # Windows 版依赖包（Python 3.11 / win_amd64）
```

## 在联网构建机打包

构建机需安装 Git、Node.js/npm 和 Python（含 pip），并能访问 npm 与 PyPI。在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\win10\package.ps1
```

脚本会重新构建前端、下载 Python 3.11 / win_amd64 wheel，并在仓库根目录生成
`CaseGen-intranet-win10-py311-YYYYMMDD.zip` 及对应 `.sha256`。目标文件已存在时可加
`-Force`，也可用 `-OutputPath <路径.zip>` 指定输出位置。

## 部署步骤（Win10 目标机器）

### 前置要求

1. 安装 **64 位 Python 3.11**（不要使用 3.12/3.13；官网 python.org 下载，安装时务必勾选
   **"Add python.exe to PATH"**）。
   > 本包附带的后端依赖（`backend_wheels/`）为 **cp311 / win_amd64** 版本，
   > 仅支持 64 位 Python 3.11。
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
- **Python 版本固定为 3.11 x64**：`install.bat` 会拒绝其它 Python 版本或 32 位解释器。

## 打包脚本验证记录（2026-09-01）

- Windows PowerShell 5.1 完整打包通过，前端 `npm run build` 通过。
- 生成 32 个 CPython 3.11 / win_amd64 wheel，包内断网依赖解析通过。
- ZIP 关键文件、排除项和 SHA256 校验通过。
