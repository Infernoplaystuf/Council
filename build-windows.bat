@echo off
REM ============================================================
REM  build-windows.bat — produce a standalone DatabaseGrabber.exe
REM
REM  Run this ONCE on a Windows machine that has Python 3.10+ .
REM  It creates an isolated build venv, installs the dependencies
REM  plus PyInstaller, and packages everything into a single .exe.
REM  The resulting dist\DatabaseGrabber.exe needs NO Python on the
REM  machines you give it to.
REM ============================================================
setlocal enableextensions

set "HERE=%~dp0"
cd /d "%HERE%"

where python >nul 2>&1
if errorlevel 1 (
    echo [build] Python not found on PATH. Install Python 3.10+ from
    echo         https://www.python.org/downloads/ (tick "Add to PATH"^), then re-run.
    pause
    exit /b 1
)

echo [build] Creating build virtual environment (.build-venv)...
python -m venv .build-venv || (echo venv creation failed & pause & exit /b 1)

call ".build-venv\Scripts\activate.bat"
echo [build] Installing dependencies + PyInstaller...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)

echo [build] Packaging with PyInstaller...
pyinstaller --noconfirm database_grabber.spec || (echo PyInstaller failed & pause & exit /b 1)

echo.
echo [build] DONE. Your standalone app is:
echo         %HERE%dist\DatabaseGrabber.exe
echo         Copy that single file anywhere — no Python needed to run it.
echo.
pause
