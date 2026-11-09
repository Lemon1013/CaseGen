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

rem --- DSH agent plane (line A) -----------------------------------------
rem This launch command has no --env-file, so the backend only sees
rem config.py defaults plus the variables set here (or system-wide).
rem Set AGENT_ENABLED=false when the target machine has no DSH runtime:
rem /api/agent/* then answers 410 and the frontend degrades softly.
if not defined DSH_BASE_URL set DSH_BASE_URL=http://127.0.0.1:31800/api/v1
if not defined AGENT_ENABLED set AGENT_ENABLED=true
if not defined AGENT_MAX_CONCURRENT set AGENT_MAX_CONCURRENT=4
rem "~" expands to the Windows user profile; keeps workspaces out of the repo.
if not defined AGENT_WORKSPACE_ROOT set AGENT_WORKSPACE_ROOT=~/.casegen/agent-workspace
if not defined AGENT_FIRST_TOKEN_TIMEOUT_SEC set AGENT_FIRST_TOKEN_TIMEOUT_SEC=600
if not defined AGENT_STALL_TIMEOUT_SEC set AGENT_STALL_TIMEOUT_SEC=300
if not defined AGENT_STAGE_MAX_CALLS set AGENT_STAGE_MAX_CALLS=4
if not defined PIPELINE_AGENT_STAGES set PIPELINE_AGENT_STAGES=
if not defined AGENT_SKILLS_DIR set AGENT_SKILLS_DIR=.agents/skills
echo 启动中... 浏览器打开 http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
