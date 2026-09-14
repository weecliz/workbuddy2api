@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title workbuddy2api - converter

rem ============================================================
rem  workbuddy2api  Run mode A: local direct gateway
rem
rem  Requires: Python only. MySQL / Redis NOT needed.
rem  Prereq  : WorkBuddy / CodeBuddy desktop app is logged in.
rem
rem  Usage:
rem    double-click this file
rem    start_converter.bat                       use default args
rem    start_converter.bat --port 9000           override port
rem    start_converter.bat --api-key mysecret    add local api key
rem    start_converter.bat --no-compact          keep fuller system prompt
rem
rem  Env vars:
rem    CONVERTER_PYTHON       path to python.exe (auto-detected by default)
rem    CONVERTER_PORT         default 8787
rem    CONVERTER_DESENSITIZE  set to 0 to disable desensitize
rem ============================================================

if not defined CONVERTER_PORT set "CONVERTER_PORT=8787"

echo.
echo ============================================================
echo   workbuddy2api   converter   local direct gateway
echo ------------------------------------------------------------
echo   listen : http://127.0.0.1:%CONVERTER_PORT%
echo   routes : /v1/chat/completions  /v1/responses  /v1/messages
echo            /v1/models  /v1/balance  /health
echo ============================================================
echo.

rem ---------- 0. project file check ----------
if not exist "core\converter.py" (
    echo [ERROR] core\converter.py not found in the current directory.
    echo         Put this script in the workbuddy2api project root.
    echo.
    pause
    exit /b 2
)

rem ---------- 1. locate python ----------
rem Order matters: the project's own venv must win over whatever `python` happens
rem to be on PATH, otherwise a bare system interpreter (without the project deps)
rem gets picked and the script fails for no good reason.
set "PY="
if defined CONVERTER_PYTHON set "PY=%CONVERTER_PYTHON%"

if not defined PY for %%P in (
    "%~dp0..\.venv\Scripts\python.exe"
    "%~dp0..\venv\Scripts\python.exe"
    "%~dp0..\env\Scripts\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY for /f "delims=" %%W in ('where python 2^>nul') do if not defined PY set "PY=%%W"

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         Install Python 3.10+ or set CONVERTER_PYTHON to your python.exe.
    echo.
    pause
    exit /b 2
)
echo [1/3] interpreter : %PY%

rem ---------- 2. dependency check ----------
"%PY%" -c "import fastapi, uvicorn, httpx" 1>nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] This interpreter is missing runtime dependencies.
    echo         Run the command below first, then retry:
    echo.
    echo            "%PY%" -m pip install -r requirements.txt
    echo.
    echo         Or set CONVERTER_PYTHON to an interpreter that already has them.
    echo.
    pause
    exit /b 2
)
echo [2/3] dependency  : OK

rem ---------- 3. build args and start ----------
set "RUN_ARGS=--port %CONVERTER_PORT%"
if not "%CONVERTER_DESENSITIZE%"=="0" set "RUN_ARGS=%RUN_ARGS% --desensitize"
set "RUN_ARGS=%RUN_ARGS% --log converter.log"

echo [3/3] command     : python -m core.converter %RUN_ARGS% %*
echo.
echo       Press Ctrl+C to stop the service.
echo.

"%PY%" -m core.converter %RUN_ARGS% %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo ============================================================
    echo   converter exited with code %RC%
    echo ------------------------------------------------------------
    echo   Common causes:
    echo     1. Port %CONVERTER_PORT% is already in use
    echo        try: start_converter.bat --port 9000
    echo     2. Desktop app not logged in, or auth file missing
    echo     3. Token expired - re-login the desktop app and retry
    echo   Full log: converter.log
    echo ============================================================
    pause
)

endlocal & exit /b %RC%
