@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title workbuddy2api - admin

rem ============================================================
rem  workbuddy2api  Run mode B: all-in-one admin platform
rem
rem  One process on port 8790 serving:
rem    /admin              management dashboard
rem    /v1/chat/...        shared gateway (api key + quota)
rem    /gw/v1/...          embedded converter (desktop login)
rem
rem  Requires: Python + MySQL 8.  Redis is OPTIONAL - admin
rem            auto-degrades to in-process rate limiting when
rem            redis is unreachable.
rem  Prereq  : .env in the project root. Auto-created from
rem            .env.example on first run - review it afterwards.
rem
rem  Usage:
rem    double-click this file
rem    start_admin.bat                  use default args
rem    start_admin.bat --port 8080      override the listening port
rem
rem  Env vars:
rem    CONVERTER_PYTHON   path to python.exe (auto-detected by default)
rem    ADMIN_PORT         default 8790
rem ============================================================

set "SHOW_PORT=8790"
if defined ADMIN_PORT set "SHOW_PORT=%ADMIN_PORT%"

echo.
echo ============================================================
echo   workbuddy2api   admin + gateway
echo ------------------------------------------------------------
echo   dashboard  : http://127.0.0.1:%SHOW_PORT%/admin
echo   OpenAI API : http://127.0.0.1:%SHOW_PORT%/v1/chat/completions
echo   Responses  : http://127.0.0.1:%SHOW_PORT%/v1/responses
echo   Claude API : http://127.0.0.1:%SHOW_PORT%/v1/messages
echo   converter  : http://127.0.0.1:%SHOW_PORT%/gw/v1/...
echo ------------------------------------------------------------
echo   client base_url:
echo     OpenAI SDK   base_url = http://127.0.0.1:%SHOW_PORT%/v1
echo     Claude Code  base_url = http://127.0.0.1:%SHOW_PORT%
echo   All /v1/* endpoints share the same API Keys and quota;
echo   API Key is created in the dashboard, API Keys page.
echo   /v1/messages maps claude-* model names automatically.
echo ============================================================
echo.

rem ---------- 0. project file check ----------
if not exist "main.py" (
    echo [ERROR] main.py not found in the current directory.
    echo         Put this script in the workbuddy2api project root.
    echo.
    pause
    exit /b 2
)

rem ---------- 1. .env check ----------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [WARN] .env was missing - created from .env.example.
        echo        Please review it before exposing this service:
        echo          - ADMIN_DATABASE_URL must match your MySQL
        echo          - set a strong ADMIN_PASSWORD
        echo          - set a strong ADMIN_JWT_SECRET
        echo.
    ) else (
        echo [ERROR] Neither .env nor .env.example was found.
        echo.
        pause
        exit /b 2
    )
)

rem ---------- 2. locate python ----------
set "PY="
if defined CONVERTER_PYTHON set "PY=%CONVERTER_PYTHON%"

if not defined PY for %%P in (
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    "%~dp0.venv\Scripts\python.exe"
    "%~dp0venv\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY for /f "delims=" %%W in ('where python 2^>nul') do if not defined PY set "PY=%%W"

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         Install Python 3.10+ or set CONVERTER_PYTHON to your python.exe.
    echo.
    pause
    exit /b 2
)
echo [1/4] interpreter : %PY%

rem ---------- 3. dependency check ----------
rem The redis package is intentionally NOT required: admin degrades to
rem in-process rate limiting when redis is missing or unreachable.
"%PY%" -c "import fastapi, uvicorn, httpx, sqlalchemy, pymysql, jwt, dotenv" 1>nul 2>nul
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
echo [2/4] dependency  : OK

rem ---------- 4. MySQL reachability pre-check ----------
rem Probe exit codes: 0 = reachable, or the url is not mysql at all.
rem                    2 = mysql url but the host/port refuses connection.
"%PY%" -c "import sys;sys.path.insert(0,'.');from admin.config import settings;import socket,urllib.parse;url=settings.DATABASE_URL;parsed=urllib.parse.urlparse(url);is_mysql=url.startswith('mysql');sock=socket.socket();sock.settimeout(2);rc=0 if not is_mysql else (0 if sock.connect_ex((parsed.hostname or '127.0.0.1',parsed.port or 3306))==0 else 2);sys.exit(rc)" 1>nul 2>nul
if errorlevel 1 (
    echo [3/4] database    : UNREACHABLE
    echo.
    echo [WARN] Cannot reach MySQL at the address in ADMIN_DATABASE_URL.
    echo        Startup will most likely fail. Check that the MySQL
    echo        service is running, and that host / port / user /
    echo        password in .env are correct.
    echo.
) else (
    echo [3/4] database    : reachable
)

rem ---------- 5. start ----------
echo [4/4] command     : main.py %*
echo.
echo       Press Ctrl+C to stop the service.
echo.

"%PY%" main.py %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo ============================================================
    echo   main.py exited with code %RC%
    echo ------------------------------------------------------------
    echo   Common causes:
    echo     1. MySQL not running, or ADMIN_DATABASE_URL in .env is
    echo        wrong. Note that special characters in the password
    echo        must be url-encoded, for example p@ss becomes p%%40ss
    echo     2. Port %SHOW_PORT% is already in use
    echo        try: start_admin.bat --port 8080
    echo     3. Admin dependencies missing
    echo        run: pip install -r requirements.txt
    echo   Logs: logs\admin.log and logs\converter-embedded.log
    echo ============================================================
    pause
)

endlocal & exit /b %RC%
