@echo off
REM ============================================================
REM  setup.bat - one-command installer for Data's Inferno / Council
REM
REM  Usage:    setup.bat            (interactive)
REM            setup.bat --yes      (accept all defaults)
REM            setup.bat --help     (full options)
REM
REM  Finds a working Python on PATH and hands off to setup_council.py.
REM  The Python script handles everything else (hardware probe, conda
REM  install, env creation, dependency install, smoke test).
REM
REM  IMPORTANT: this script PAUSES on exit so the cmd window stays
REM  open. Double-clicking setup.bat without that would close the
REM  window the instant Python exited — even on success — and the
REM  user would have no idea what happened. Use the --no-pause flag
REM  if you're running from an interactive shell where pause is
REM  annoying.
REM ============================================================
setlocal

REM Filter out --no-pause from the args we forward to Python.
set PAUSE_ON_EXIT=1
set FORWARD_ARGS=
:argparse
if "%~1"=="" goto done_args
if /i "%~1"=="--no-pause" (
    set PAUSE_ON_EXIT=0
    shift
    goto argparse
)
set FORWARD_ARGS=%FORWARD_ARGS% %1
shift
goto argparse
:done_args

REM Locate Python. Try `py -3` first (Windows launcher, picks the
REM newest installed Python 3.x), then plain `python`.
set PYTHON_EXE=
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON_EXE=py -3
    goto launch
)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON_EXE=python
    goto launch
)

echo.
echo [setup.bat] No Python on PATH.
echo.
echo   Install Python 3.11+ from https://www.python.org/downloads/
echo   During install, CHECK "Add python.exe to PATH" on the first
echo   wizard page. Then re-run setup.bat.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1

:launch
%PYTHON_EXE% "%~dp0setup_council.py" %FORWARD_ARGS%
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo [setup.bat] Setup finished cleanly.
) else (
    echo [setup.bat] Setup exited with code %RC%.
    if "%RC%"=="2" echo                  ^(code 2 = conda missing — see message above^)
    if "%RC%"=="3" echo                  ^(code 3 = a step failed — see the "Install summary" table^)
    echo.
    echo   • Scroll up to read the "Install summary" — failed steps are marked with a red X.
    echo   • Each failed step prints a "suggested fix" block with the most common causes.
    echo   • Re-running setup.bat skips steps that already succeeded.
    echo   • To start clean:  setup.bat --reinstall
)
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b %RC%
