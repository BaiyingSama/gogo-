@echo off
rem ===========================================================
rem  Wake it up once, manually.
rem
rem  Usage:
rem    run-awake.cmd            wake once (asks what to do, sleeps)
rem    run-awake.cmd --check    show remaining minutes today
rem    run-awake.cmd --prompt   print the prompt only, costs nothing
rem    run-awake.cmd --relay    deliver the outbox
rem
rem  To wake it on a schedule, see the README section about cron /
rem  Windows Task Scheduler.
rem
rem  NOTE: this file is intentionally ASCII-only.
rem  Chinese text in a .cmd file gets mangled under the GBK console
rem  codepage (a multi-byte char can swallow the line ending, merging
rem  lines, and then comments get executed as commands). Keep it ASCII.
rem ===========================================================

setlocal
chcp 65001 >nul
rem Force UTF-8 on the Python side too, otherwise Chinese output comes out
rem as GBK and turns into mojibake in a UTF-8 console (and vice versa).
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if "%FEIYU_HOME%"=="" set "FEIYU_HOME=%USERPROFILE%\.feiyu-home"
if not exist "%FEIYU_HOME%" (
    echo [x] FEIYU_HOME not found: %FEIYU_HOME%
    echo     Create it first, or set the env var.
    exit /b 1
)

if "%PYTHON%"=="" set "PYTHON=python"

"%PYTHON%" "%~dp0feiyu.py" %*
set RC=%ERRORLEVEL%

endlocal & exit /b %RC%
