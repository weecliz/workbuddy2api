@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title workbuddy2api - install Windows service

rem ============================================================
rem  Install the workbuddy2api admin platform as a Windows service.
rem
rem  Steps performed:
rem    1. check this script is running as Administrator
rem    2. locate a usable Python interpreter
rem    3. check runtime dependencies
rem    4. install pywin32 when missing
rem    5. register the service, set auto start, start it
rem
rem  The service entry point is service_admin.py, NOT start_admin.bat.
rem  A .bat cannot be a service image and it blocks on pause, so it
rem  stays for interactive debugging only.
rem
rem  Usage: right click this file, then "Run as administrator".
rem
rem  Env vars:
rem    CONVERTER_PYTHON   path to python.exe, auto-detected by default
rem ============================================================

rem ---------- 0. must be elevated ----------
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Administrator privileges are required.
    echo         Close this window, then right click install_service.bat
    echo         and choose "Run as administrator".
    echo.
    pause
    exit /b 5
)

rem ---------- 1. project file check ----------
if not exist "service_admin.py" (
    echo [ERROR] service_admin.py not found in the current directory.
    echo         Put this script in the workbuddy2api project root.
    echo.
    pause
    exit /b 2
)

rem ---------- 2. locate python ----------
set "PY="
if defined CONVERTER_PYTHON set "PY=%CONVERTER_PYTHON%"

if not defined PY for /f "delims=" %%W in ('where python 2^>nul') do if not defined PY set "PY=%%W"

if not defined PY for %%P in (
    "%~dp0..\.venv\Scripts\python.exe"
    "%~dp0..\venv\Scripts\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         Install Python 3.10+ or set CONVERTER_PYTHON to your python.exe.
    echo.
    pause
    exit /b 2
)
echo [1/4] interpreter : %PY%

rem ---------- 3. runtime dependencies ----------
"%PY%" -c "import fastapi, uvicorn, httpx, sqlalchemy, pymysql, jwt, dotenv" 1>nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] This interpreter is missing runtime dependencies.
    echo         Run this command first, then retry:
    echo.
    echo            "%PY%" -m pip install -r requirements.txt
    echo.
    echo         Or set CONVERTER_PYTHON to an interpreter that has them.
    echo.
    pause
    exit /b 2
)
echo [2/4] dependency  : OK

rem ---------- 4. pywin32 ----------
"%PY%" -c "import win32serviceutil" 1>nul 2>nul
if errorlevel 1 (
    echo [3/4] pywin32     : installing ...
    "%PY%" -m pip install pywin32
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install pywin32. Check network or proxy settings.
        echo.
        pause
        exit /b 3
    )
) else (
    echo [3/4] pywin32     : OK
)

rem ---------- 5. register and start ----------
echo [4/4] registering service ...
"%PY%" "service_admin.py" install
if errorlevel 1 (
    echo.
    echo [ERROR] Service registration failed. See messages above.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting the service ...
"%PY%" "service_admin.py" start

echo.
echo ============================================================
echo   Done. Open services.msc and look for "workbuddy2api".
echo ------------------------------------------------------------
echo   dashboard  : http://127.0.0.1:8790/admin
echo   gateway    : http://127.0.0.1:8790/v1/chat/completions
echo   service log: logs\service.log
echo   uninstall  : uninstall_service.bat, run as administrator
echo ============================================================
echo.
pause
endlocal
exit /b 0
