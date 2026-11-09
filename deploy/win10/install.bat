@echo off
chcp 65001 >nul
rem ============================================================
rem  CaseGen Win10 离线部署 - 后端依赖安装
rem  本机不联网也可安装：所有 wheel 已随包提供。
rem ============================================================
setlocal

cd /d "%~dp0..\..\backend"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 64 位 Python 3.11（安装时勾选 Add python.exe to PATH）。
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,11) and sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    echo [错误] 本离线包仅支持 64 位 Python 3.11。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [1/2] 虚拟环境 .venv 已存在，跳过创建。
)

echo [2/2] 离线安装后端依赖（不访问网络）...
".venv\Scripts\python.exe" -m pip install --no-index --find-links "%~dp0backend_wheels" -r "%~dp0requirements-offline.txt"
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查 deploy\win10\backend_wheels 目录是否完整。
    pause
    exit /b 1
)

echo.
echo 安装完成。双击 run.bat 启动服务。
pause
