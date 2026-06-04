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
REM ============================================================
setlocal

REM Locate Python. Try `py -3` first (Windows launcher, picks the
REM newest installed Python 3.x), then plain `python`.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%~dp0setup_council.py" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%~dp0setup_council.py" %*
    exit /b %ERRORLEVEL%
)

echo.
echo [setup.bat] No Python on PATH.
echo Install Python 3.11+ from https://www.python.org/downloads/
echo During install, CHECK "Add python.exe to PATH" on the first wizard page.
echo Then re-run setup.bat.
echo.
exit /b 1
