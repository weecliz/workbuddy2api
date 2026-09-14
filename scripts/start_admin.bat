@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title workbuddy2api - admin

rem ============================================================
rem  workbuddy2api  Run mode B: all-in-one admin platform
rem
rem  One process on port 8790 serving:
rem    /admin              management dashboard
rem    /v1/chat/...        shared gateway (api key + quota)
rem    /gw/v1/...          embedded converter (desktop login)
rem
rem  Requires: Python 3.10+.  Database is configurable and defaults
rem            to SQLite (zero deps); set ADMIN_DB_TYPE=mysql|db2 in
rem            .env for a server DB.  Redis is OPTIONAL - admin
rem            auto-degrades to in-process rate limiting when
rem            redis is unreachable.
rem  Prereq  : .env in the project root. Auto-created from
rem            .env.example on first run - review it afterwards.
rem
rem  Usage:
rem    double-click this file
rem    start_admin.bat                  use default args (.env decides the DB)
rem    start_admin.bat --port 8080      override the listening port
rem    start_admin.bat --db-type sqlite --db-name ./data/wb.db
rem    start_admin.bat --db-type db2 --db-name WBADMIN --db-schema WBADMIN
rem    start_admin.bat --list-db-types  show supported database types
rem
rem  Database args are forwarded to main.py and override .env for this run:
rem    --db-type mysql|db2|sqlite   --db-host  --db-port  --db-user
rem    --db-password  --db-name  --db-schema  --db-options
rem
rem  Env vars:
rem    CONVERTER_PYTHON   path to python.exe (auto-detected by default)
rem    ADMIN_PORT         default 8790
rem    ADMIN_DB_TYPE      sqlite|mysql|db2 (default sqlite; .env wins over it)
rem ============================================================

set "SHOW_PORT=8790"
if defined ADMIN_PORT set "SHOW_PORT=%ADMIN_PORT%"

rem Show which database this run will use, mimicking main.py's precedence:
rem   --db-type on the command line  >  ADMIN_DB_TYPE in the environment  >
rem   ADMIN_DB_TYPE in .env  >  DEFAULT_DB_TYPE (sqlite)
rem Both "--db-type db2" and "--db-type=db2" spellings are handled.
rem NOTE: inside a for-block, %PREV% is expanded when the block is parsed, so the
rem "previous argument" trick needs delayed expansion (enabled here on purpose).
set "SHOW_DB=sqlite"
set "SHOW_DB_SRC=database default (DEFAULT_DB_TYPE)"

rem Read .env WITHOUT loading it into this process. The file may declare
rem ADMIN_DB_TYPE more than once (one active line plus commented-out
rem alternatives), so the LAST active match wins - that mirrors how
rem python-dotenv resolves duplicates. Inline comments (`K=V  # note`) are cut.
rem Note the two-stage for-loop: the inner one normalises the value so the
rem outer one can export it, which avoids endlocal killing !delayed! vars.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%K in (".env") do (
        if /i "%%K"=="ADMIN_DB_TYPE" if not "%%L"=="" call :set_db_from_value "%%L"
    )
)
goto :db_banner_done

:set_db_from_value
rem %~1 may carry an inline comment and/or quotes; take the first bare token.
for /f "tokens=1 delims=# 	" %%V in ("%~1") do set "SHOW_DB=%%~V"
set "SHOW_DB_SRC=.env"
goto :eof

:db_banner_done

rem An explicit environment variable beats .env (dotenv does not override it).
if defined ADMIN_DB_TYPE set "SHOW_DB=%ADMIN_DB_TYPE%" & set "SHOW_DB_SRC=environment"

rem The command line beats everything.
setlocal EnableDelayedExpansion
set "PREV="
for %%A in (%*) do (
    if /i "!PREV!"=="--db-type" set "SHOW_DB=%%~A" & set "SHOW_DB_SRC=command line"
    if /i "%%~A"=="--db-type=mysql"  set "SHOW_DB=mysql"  & set "SHOW_DB_SRC=command line"
    if /i "%%~A"=="--db-type=db2"    set "SHOW_DB=db2"    & set "SHOW_DB_SRC=command line"
    if /i "%%~A"=="--db-type=sqlite" set "SHOW_DB=sqlite" & set "SHOW_DB_SRC=command line"
    set "PREV=%%~A"
)
endlocal & set "SHOW_DB=%SHOW_DB%"

echo.
echo ============================================================
echo   workbuddy2api   admin + gateway
echo ------------------------------------------------------------
echo   database   : %SHOW_DB%
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
rem Order matters: the project's own venv must win over whatever `python` happens
rem to be on PATH. Scanning PATH first used to pick the bare system interpreter
rem (which lacks fastapi/sqlalchemy), so the bat reported "missing dependencies"
rem even though .venv was sitting right there fully populated.
set "PY="
if defined CONVERTER_PYTHON set "PY=%CONVERTER_PYTHON%"

rem 2a. project venv first (relative to this script, so it works from anywhere)
if not defined PY for %%P in (
    "%~dp0..\.venv\Scripts\python.exe"
    "%~dp0..\venv\Scripts\python.exe"
    "%~dp0..\env\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

rem 2b. other known locations
if not defined PY for %%P in (
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

rem 2c. last resort: whatever is first on PATH
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
rem The DB driver is checked per --db-type / ADMIN_DB_TYPE by the probe below:
rem   mysql -> pymysql, db2 -> ibm_db_sa, sqlite -> nothing (stdlib).
"%PY%" -c "import fastapi, uvicorn, httpx, sqlalchemy, jwt, dotenv" 1>nul 2>nul
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
rem Forward the db args to the probe so it resolves the same config as main.py.
set "DB_PROBE=%~dp0db_probe.py"
"%PY%" "%DB_PROBE%" %* 1>nul 2>nul
rem NOTE: this must be the bare `if errorlevel N` form. Inside a parenthesised
rem block %ERRORLEVEL% is expanded at parse time, so it would always be stale.
if errorlevel 3 (
    echo.
    echo [ERROR] The selected database driver is not installed.
    echo         See the message below for the exact pip command.
    echo.
    "%PY%" "%DB_PROBE%" %*
    echo.
    pause
    exit /b 2
)
echo [2/4] dependency  : OK

rem ---------- 4. database reachability pre-check ----------
rem The probe already ran above; re-running it quietly gives us a fresh code.
rem                  exit 2 = host/port refuses connection (mysql / db2)
rem                  exit 0 = reachable, or sqlite (no host/port to check)
"%PY%" "%DB_PROBE%" %* 1>nul 2>nul
if errorlevel 2 (
    echo [3/4] database    : UNREACHABLE
    echo.
    echo [WARN] Cannot reach %SHOW_DB% at the configured address.
    echo        Startup will most likely fail. Check that the database
    echo        service is running, and that host / port / user /
    echo        password are correct.
    echo.
    "%PY%" "%DB_PROBE%" %*
    echo.
) else (
    echo [3/4] database    : %SHOW_DB% ready
)

rem ---------- 5. start ----------
echo [4/4] command     : main.py %*
echo.
echo       Press Ctrl+C to stop the service.
echo.

"%PY%" main.py %*
set "RC=%ERRORLEVEL%"

rem Exit codes: -1 / -1073741510 mean the process was interrupted (Ctrl+C or the
rem console window was closed) - that is the normal way to stop a foreground run,
rem not a crash. Only report the troubleshooting block for a real failure.
if "%RC%"=="-1" (
    echo.
    echo ============================================================
    echo   Stopped (interrupted by Ctrl+C or window close).
    echo ------------------------------------------------------------
    echo   database   : %SHOW_DB%
    echo   Logs       : logs\admin.log and logs\converter-embedded.log
    echo ============================================================
    echo.
    pause
    endlocal & exit /b 0
)
if "%RC%"=="-1073741510" (
    echo.
    echo ============================================================
    echo   Stopped (console window closed). See logs\admin.log.
    echo ============================================================
    echo.
    pause
    endlocal & exit /b 0
)

if not "%RC%"=="0" (
    echo.
    echo ============================================================
    echo   main.py exited with code %RC%
    echo ------------------------------------------------------------
    echo   Current database : %SHOW_DB%  (from %SHOW_DB_SRC%)
    echo.
    echo   Common causes:
    echo     1. The database is unreachable or misconfigured.
    echo        - sqlite: ADMIN_DB_NAME must point at a writable path
    echo        - mysql : MySQL service must be running, and any special
    echo          characters in the password must be url-encoded
    echo          (p@ss becomes p%%40ss) if you use ADMIN_DATABASE_URL
    echo        - db2   : DB2 instance must be listening on the port
    echo        Check the effective config with:
    echo          python -m admin.db_config
    echo        Check driver + connectivity with:
    echo          python scripts\db_probe.py
    echo     2. Port %SHOW_PORT% is already in use
    echo        try: start_admin.bat --port 8080
    echo        or free it with: stop_admin.bat --port %SHOW_PORT%
    echo     3. Admin dependencies missing
    echo        run: pip install -r requirements.txt
    echo   Logs: logs\admin.log and logs\converter-embedded.log
    echo ============================================================
    pause
)

endlocal & exit /b %RC%
