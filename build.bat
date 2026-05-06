@echo off
REM ============================================================
REM Council  —  Windows build script
REM ============================================================
REM Produces dist\Council\Council.exe and supporting files.
REM Pre-reqs:
REM     - Active Python 3.11 environment
REM     - PyInstaller installed:  pip install pyinstaller
REM ============================================================

setlocal

echo === Cleaning previous build artefacts ===
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo === Verifying PyInstaller is available ===
python -m PyInstaller --version 1>NUL 2>NUL
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller. Aborting.
        exit /b 1
    )
)

echo === Running PyInstaller ===
python -m PyInstaller council.spec --noconfirm --clean

if errorlevel 1 (
    echo BUILD FAILED.
    exit /b 1
)

echo.
echo === Build complete ===
echo Bundle location: %CD%\dist\Council
echo Executable:      %CD%\dist\Council\Council.exe
echo.
echo To create a distributable installer, run an installer builder
echo like Inno Setup or NSIS against dist\Council.

endlocal
