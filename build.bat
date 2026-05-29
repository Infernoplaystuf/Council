@echo off
REM ============================================================
REM Anvil — Windows build script
REM ============================================================
REM Produces dist\Anvil\Anvil.exe and supporting bundle files.
REM Pre-reqs:
REM     - Active Python 3.11 environment
REM     - PyInstaller installed:  pip install pyinstaller
REM
REM Expect the build to take 5–10 minutes the first time —
REM llama-cpp-python, chromadb, sentence-transformers, matplotlib,
REM and pandas all contribute large native binaries.
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
python -m PyInstaller anvil.spec --noconfirm --clean

if errorlevel 1 (
    echo BUILD FAILED.
    exit /b 1
)

echo.
echo === Build complete ===
echo Bundle location: %CD%\dist\Anvil
echo Executable:      %CD%\dist\Anvil\Anvil.exe
echo.
echo Launch with a double-click, or from the command line:
echo     dist\Anvil\Anvil.exe
echo.
echo To create a distributable installer, run Inno Setup or NSIS
echo against dist\Anvil.

endlocal
