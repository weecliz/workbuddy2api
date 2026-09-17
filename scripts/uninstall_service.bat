@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
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
rem Order: CONVERTER_PYTHON > project venv > other known spots > PATH.
rem The project venv must come BEFORE the PATH lookup. A bare
rem "where python" frequently resolves to a system interpreter that lacks
rem pywin32 (it is not a project dependency), so the service operation would
rem abort with "缺少 pywin32" even though .venv, sitting right there, has it.
rem Candidates that cannot do the job are skipped rather than chosen blindly
rem (see :try_py below), so a bad PATH entry no longer blocks the operation.
set "PY="
if defined CONVERTER_PYTHON set "PY=%CONVERTER_PYTHON%"

if not defined PY for %%P in (
    "%~dp0..\.venv\Scripts\python.exe"
    "%~dp0..\venv\Scripts\python.exe"
    "%~dp0..\env\Scripts\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) do if not defined PY if exist %%P call :try_py "%%~P"

rem Last resort: whatever is first on PATH.
if not defined PY for /f "delims=" %%W in ('where python 2^>nul') do if not defined PY call :try_py "%%~W"

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         Need Python 3.10+ with the runtime dependencies AND pywin32.
    echo         Install them into the project venv, or point CONVERTER_PYTHON
    echo         at a suitable interpreter.
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

rem ------------------------------------------------------------
rem  :try_py  <path-to-python>
rem  Probe a candidate interpreter and set PY only if it can run the service
rem  script. pywin32 is the decisive check: it is NOT a project dependency, so
rem  interpreters that never had it installed are simply skipped and the search
rem  moves on to the next candidate (instead of failing the whole operation).
rem  %%W / %%P from the caller's for-loop are not visible in here, hence the
rem  "call :try_py" indirection.
rem ------------------------------------------------------------
:try_py
"%~1" -c "import win32serviceutil, fastapi, sqlalchemy" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof
