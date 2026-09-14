@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
title workbuddy2api - stop

rem ============================================================
rem  workbuddy2api  Stop the all-in-one admin platform
rem
rem  Counterpart of start_admin.bat. Finds whatever is actually
rem  listening on the port, asks it to shut down gracefully
rem  (SIGTERM / Ctrl+C equivalent), and force-kills it only if it
rem  refuses to go away.
rem
rem  This matters because the app runs as a small process tree:
rem      main.py  ->  uvicorn (admin, port 8790)
rem  Killing the parent leaves the child holding the port, so the
rem  script walks the whole tree instead of one PID.
rem
rem  Usage:
rem    double-click this file
rem    stop_admin.bat                     stop the default port 8790
rem    stop_admin.bat --port 8080         stop a different port
rem    stop_admin.bat --force             skip the graceful attempt
rem    stop_admin.bat --list              only show what is running
rem
rem  Env vars:
rem    ADMIN_PORT   default 8790
rem ============================================================

set "PORT=8790"
if defined ADMIN_PORT set "PORT=%ADMIN_PORT%"
set "FORCE=0"
set "LISTONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--force" (
    set "FORCE=1"
    shift
    goto parse_args
)
if /i "%~1"=="--list" (
    set "LISTONLY=1"
    shift
    goto parse_args
)
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h" goto usage
echo [ERROR] Unknown option: %~1
echo         Run "stop_admin.bat --help" for usage.
echo.
pause
exit /b 2

:usage
echo.
echo Usage: stop_admin.bat [--port N] [--force] [--list]
echo.
echo   --port N   port to stop (default 8790, or ADMIN_PORT)
echo   --force    skip the graceful shutdown attempt
echo   --list     only report what is running, do not stop anything
echo.
pause
exit /b 0

:args_done

echo.
echo ============================================================
echo   workbuddy2api   stop
echo ------------------------------------------------------------
echo   port       : %PORT%
echo ============================================================
echo.

rem ---------- 1. must be able to inspect processes ----------
tasklist >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot query the process list.
    echo         Close this window, then right click stop_admin.bat
    echo         and choose "Run as administrator".
    echo.
    pause
    exit /b 5
)

rem ---------- 2. find the PID owning the port ----------
rem netstat gives us every connection touching the port; LISTENING rows have
rem the server PID in the last column. `findstr` on ":8790 " with the trailing
rem space avoids matching e.g. :87900.
set "PIDS="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    set "PIDS=!PIDS! %%P"
)

if not defined PIDS (
    echo [1/3] listening   : nothing on port %PORT%
    echo.
    echo The service does not appear to be running.
    echo If you started it in a foreground window, just press Ctrl+C there
    echo or close that window instead.
    echo.
    pause
    exit /b 0
)
rem NOTE: never write an unescaped ")" inside an echo inside a parenthesised
rem block - cmd closes the block on it and reports "<next token> was unexpected".
echo [1/3] listening   : found PID[s]!PIDS!

if "%LISTONLY%"=="1" (
    echo.
    echo --list given, not stopping anything. Running process list below:
    echo.
    for %%P in (!PIDS!) do tasklist /fi "PID eq %%P" /fo table /nh 2>nul
    echo.
    pause
    exit /b 0
)

rem ---------- 3. collect the process tree ----------
rem The app runs as a small tree:  main.py  ->  uvicorn (binds the port).
rem netstat reports whoever holds the socket = uvicorn. Killing only that PID
rem leaves main.py behind as an orphan, so the walk goes BOTH ways:
rem   DESC = descendants of the listener   (depth first, deepest first)
rem   ANC  = ancestor chain of the listener (nearest first)
rem Ordering matters: close children first, then the listener, then its parents
rem (main.py), so nothing is left orphaned and the port is released cleanly.
rem
rem The lookups live in :add_tree / :add_ancestors and are invoked with `call`
rem on purpose: %%P is a for-variable and for-variables are NOT visible inside a
rem called routine, so the PID has to be passed as a parameter.
rem DESC / ANC are accumulated separately so the final kill order is
rem descendants -> listener -> ancestors (see ALL below).
rem TARGETS is reused by :add_tree and TARGETS2 by :add_ancestors; both must
rem start empty or a previous call would leak PIDs across runs.
set "TARGETS="
set "TARGETS2="
set "DESC="
set "ANC="
if "%FORCE%"=="0" (
    for %%P in (!PIDS!) do call :add_tree %%P
    set "DESC=!TARGETS!"
)
rem Ancestors are needed even in --force mode: main.py is the parent of the
rem listener and would otherwise be left running with a dead child.
for %%P in (!PIDS!) do call :add_ancestors %%P
set "ANC=!TARGETS2!"

rem Full kill list: deepest children, then the listener, then its parents.
set "ALL=!DESC!!PIDS!!ANC!"
echo [2/3] target      :!ALL!

if "%FORCE%"=="0" (
    echo [3/3] stopping    : graceful terminate, tree included ...
    rem /T includes the child tree; without /F this is a polite terminate that
    rem the app handles via its SIGTERM handler (flushes logs, closes the pool).
    for %%P in (!PIDS!) do taskkill /T /PID %%P >nul 2>&1
    rem Give it a moment to release the port and flush logs.
    timeout /t 3 /nobreak >nul 2>&1
) else (
    echo [3/3] stopping    : --force given, skipping graceful attempt ...
)

rem ---------- 4. force kill whatever is left ----------
set "STILL="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    set "STILL=!STILL! %%P"
)

if not defined STILL goto sweep_tree
echo       still up, forcing :!STILL!
for %%P in (!STILL!) do taskkill /F /PID %%P >nul 2>&1

rem ---------- 4b. sweep the rest of the tree ----------
rem Runs even when the port is already free: main.py is not holding the port,
rem so netstat alone would never mention it and it would survive as an orphan.
rem ALL = descendants + listener + ancestors, so this also finishes off
rem anything from the graceful pass that ignored the terminate.
:sweep_tree
for %%P in (!ALL!) do taskkill /F /PID %%P >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1

rem ---------- 5. verify ----------
set "STILL="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    set "STILL=!STILL! %%P"
)

echo.
if defined STILL (
    echo [WARN] Port %PORT% is still held by PID[s]!STILL!
    echo.
    echo        Something else may own that port, or the process needs more time.
    echo        Try again with:  stop_admin.bat --port %PORT% --force
    echo.
    pause
    exit /b 1
)

:stopped

echo ============================================================
echo   stopped. port %PORT% is free.
echo ------------------------------------------------------------
echo   Logs are kept under the logs folder, see admin.log.
echo ============================================================
echo.
pause
endlocal
exit /b 0

rem ------------------------------------------------------------
rem  :add_tree  <pid>   - append this PID's DESCENDANTS (deepest
rem  first) to TARGETS.
rem
rem  Delegates to _proc_tree.ps1. The walk USED to be inline here and
rem  went through two bad revisions: a recursive wmic call (tripped
rem  "BATCH RECURSION exceeds STACK limits" because /format:csv rows
rem  were parsed as more PIDs forever), then an inline PowerShell
rem  -Command one-liner (batch does not escape { }, so the doubled
rem  braces needed for that context reached PowerShell verbatim and
rem  it died with "Unexpected token '}'"). A .ps1 file is quoted once
rem  by us and needs no escaping gymnastics.
rem
rem  %%C is a for-variable and is NOT visible inside :eof-routines,
rem  hence the "call :add_tree %%P" indirection at the call site.
rem ------------------------------------------------------------
rem NOTE: the flag is -RootPid, not -Pid ($Pid is a read-only automatic
rem variable in PowerShell and binding to it errors out).
rem
rem The for /f command MUST stay on ONE line. Splitting it with a trailing "^"
rem continuation makes cmd swallow the following lines as part of the command
rem (the continuation eats the start of the next line), which both corrupted
rem the output and broke label resolution for the routines defined after it.
rem Line length here is fine - batch has no practical limit for this.
:add_tree
for /f "usebackq tokens=1,*" %%C in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_proc_tree.ps1" -RootPid %~1 -Mode descendants 2^>nul`) do if not "%%C"=="" set "TARGETS=!TARGETS! %%C"
goto :eof

rem ------------------------------------------------------------
rem  add_ancestors <pid> - walk UP the parent chain, appending
rem  every ancestor to TARGETS2 (nearest ancestor first).
rem
rem  This is what catches main.py: it is the PARENT of the process
rem  that owns the port, so a descendant-only walk never sees it and
rem  it would survive the stop as an orphan.
rem
rem  A separate accumulator (TARGETS2, not TARGETS) keeps ancestors
rem  out of the descendants list so the caller can order the kills as
rem  descendants -> listener -> ancestors.
rem
rem  As in add_tree above, the for /f command is kept on a single line
rem  on purpose - a "^" continuation here would swallow the lines that
rem  follow and cmd would then fail to resolve the very next label.
rem ------------------------------------------------------------
:add_ancestors
for /f "usebackq tokens=1,*" %%C in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_proc_tree.ps1" -RootPid %~1 -Mode ancestors 2^>nul`) do if not "%%C"=="" set "TARGETS2=!TARGETS2! %%C"
goto :eof
