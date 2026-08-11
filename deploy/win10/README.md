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
    └── backend_wheels/      # 30 个 Windows 版依赖包（Python 3.12 / win_amd64）
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
- **停止服务**：在 run.bat 窗口按 `Ctrl+C` 或直接关闭窗口。
- **修改端口**：编辑 `run.bat`，把 `--port 8000` 改成其它端口。
- **更换 Python 版本**（如改用 3.12）：在可联网的 Windows 机器上重新下载对应版本 wheel：

  ```
  cd backend
  pip download -r requirements.txt --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all: -d ..\deploy\win10\backend_wheels
  ```

  并把 `requirements.txt` 中 `uvicorn[standard]` 改为 `uvicorn`（uvloop 不支持 Windows），
  同时清空 `backend_wheels\` 后再下载，避免新旧版本混用。

## 本机验证记录

- 后端 pytest：`185 passed`（含 Wiki spaces）
- 前端 `npm run build`：通过（含 WikiSpacesView）
- 单进程模式（后端托管 dist）：健康检查、首页、SPA 路由、静态资源、API 全部 200
- Python 3.11.14 实机验证：`185 passed`，uvicorn 启动与静态托管正常（后端 `backend/.venv311` 与 Win10 同步）
- 版本基线：`be11151`（含 Wiki spaces 2.0 与静态托管）
