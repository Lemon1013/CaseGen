@echo off
chcp 65001 >nul
rem ============================================================
rem  CaseGen Win10 单进程启动
rem  后端(8000) + 内置前端页面(frontend/dist) 由同一个服务提供。
rem  浏览器访问 http://127.0.0.1:8000
rem ============================================================
setlocal

cd /d "%~dp0..\..\backend"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境，请先双击 install.bat 安装依赖。
    pause
    exit /b 1
)

if not exist "..\frontend\dist" (
    echo [警告] 未找到前端构建 frontend\dist，仅提供 API 接口（无页面）。
    echo        正常打包应包含该目录。
)

echo 启动中... 浏览器打开 http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
