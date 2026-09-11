@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title workbuddy2api - uninstall Windows service

rem ============================================================
rem  Stop and remove the workbuddy2api Windows service.
rem
rem  Log files under logs\ are kept on purpose. Delete them
rem  manually if you want a clean slate.
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
    echo         Close this window, then right click uninstall_service.bat
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

if not defined PY for %%P in (
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    "%~dp0.venv\Scripts\python.exe"
    "%~dp0venv\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY for /f "delims=" %%W in ('where python 2^>nul') do if not defined PY set "PY=%%W"

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo.
    pause
    exit /b 2
)

rem ---------- 3. stop and remove ----------
echo Stopping and removing the service ...
"%PY%" "service_admin.py" remove
if errorlevel 1 (
    echo.
    echo [ERROR] Uninstall failed. See messages above.
    echo.
    pause
    exit /b 1
)

echo.
echo Done. The service has been removed.
echo.
pause
endlocal
exit /b 0
