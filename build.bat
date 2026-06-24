@echo off
REM ============================================================
REM Anvil (Odysseus Council)  -  Windows build script
REM ============================================================
REM Produces dist\Anvil\Anvil.exe and supporting files.
REM
REM Pre-reqs:
REM     - Python 3.11 on PATH (or a conda env activated — see
REM       installs.txt for the recommended env setup).
REM     - All runtime deps installed:  pip install -r requirements.txt
REM     - PyInstaller installed:       pip install pyinstaller
REM       (the script will pip-install it for you if missing).
REM
REM The resulting dist\Anvil\ folder is fully self-contained.
REM Copy/zip it to ship — no Python install needed on the target box.
REM
REM Expect the build to take 5-15 minutes the first time —
REM llama-cpp-python, chromadb, sentence-transformers, matplotlib,
REM and pandas all contribute large native binaries.
REM
REM What is NOT bundled (by design):
REM     - The GGUF model file. Users point COUNCIL_GGUF_PATH at one
REM       after install (or use the in-app Browse button).
REM     - The vault. Created at %USERPROFILE%\.council\ on first run.
REM ============================================================

setlocal

echo.
echo === Cleaning previous build artefacts ===
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo === Verifying Python is on PATH ===
where python >NUL 2>NUL
if errorlevel 1 (
    echo ERROR: python.exe not found on PATH.
    echo Activate your Council env or install Python 3.11 first.
    exit /b 1
)
python --version

echo.
echo === Verifying PyInstaller is available ===
python -m PyInstaller --version >NUL 2>NUL
if errorlevel 1 (
    echo PyInstaller not found. Installing into the current env...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller. Aborting.
        exit /b 1
    )
)
python -m PyInstaller --version

echo.
echo === Verifying critical runtime deps are importable ===
REM Catch missing wheels BEFORE PyInstaller spends 5 minutes on the
REM analysis pass. Each missing module is reported individually.
set "_DEP_FAIL="
for %%M in (llama_cpp pandas numpy openpyxl tkinter yaml requests) do (
    python -c "import %%M" >NUL 2>NUL
    if errorlevel 1 (
        echo   missing: %%M
        set "_DEP_FAIL=1"
    )
)
if defined _DEP_FAIL (
    echo.
    echo One or more required modules are missing.
    echo Run:  pip install -r requirements.txt
    exit /b 1
)
echo All critical deps OK.

echo.
echo === Running PyInstaller (this takes 5-15 minutes) ===
python -m PyInstaller anvil.spec --noconfirm --clean

if errorlevel 1 (
    echo.
    echo BUILD FAILED. See PyInstaller output above for the missing
    echo module / data file / native library.
    exit /b 1
)

echo.
echo === Reporting bundle size ===
for /f "tokens=*" %%S in ('powershell -NoProfile -Command "[math]::Round((Get-ChildItem -Path 'dist\Anvil' -Recurse -File ^| Measure-Object -Property Length -Sum).Sum / 1MB, 1)"') do set "BUNDLE_MB=%%S"
echo Bundle size: %BUNDLE_MB% MB

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo  Bundle:      %CD%\dist\Anvil
echo  Executable:  %CD%\dist\Anvil\Anvil.exe
echo.
echo  First-run setup on the target machine:
echo    1. Copy/extract dist\Anvil\ anywhere (e.g. C:\Programs\)
echo    2. Double-click Anvil.exe
echo    3. When prompted, point COUNCIL_GGUF_PATH at a .gguf model
echo       (Browse button inside the app does this for you).
echo.
echo  To create an installer:
echo    - Inno Setup or NSIS against dist\Anvil\
echo    - or just zip the folder
echo ============================================================

endlocal
