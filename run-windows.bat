@echo off
REM ============================================================
REM  run-windows.bat — one-command launcher for the Council app
REM  on Windows native (RTX 4080 / Ada — installs.txt Section C).
REM
REM  After setup (venv + Section C wheels) is done, this script:
REM    1. Activates .venv\Scripts\python.exe
REM    2. Sets COUNCIL_BACKEND=gguf (Ollama path is dead)
REM    3. Picks a GGUF model:
REM        - honours an externally-set COUNCIL_GGUF_PATH
REM        - else first .gguf in .\models\ or %USERPROFILE%\models\
REM    4. Exports sensible Windows defaults (GPU offload, n_ctx debug)
REM    5. Launches the GUI
REM
REM  Usage:
REM    run-windows.bat
REM    set COUNCIL_GGUF_PATH=C:\path\to\model.gguf && run-windows.bat
REM    set COUNCIL_GGUF_GPU_LAYERS=0 && run-windows.bat   :: force CPU
REM ============================================================

setlocal enableextensions enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM `run-windows.bat --check` resolves the env + reports GPU readiness, then
REM exits WITHOUT launching — a quick "did my setup work?" command.
set "CHECK_ONLY="
if /i "%~1"=="--check" set "CHECK_ONLY=1"

REM ── Resolve the Python interpreter ───────────────────────────
REM Order: (0) .council_python marker written by setup_council.py, so
REM setup->run just works with no conda-on-PATH needed; (1) COUNCIL_PYTHON
REM override; (2) a local .venv; (3) the conda 'wizardCouncil' env via
REM `conda info --base`; (4) common conda install locations.
set "PYEXE="
if exist "%SCRIPT_DIR%.council_python" (
    for /f "usebackq delims=" %%P in ("%SCRIPT_DIR%.council_python") do (
        if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
    )
)
if not defined PYEXE if defined COUNCIL_PYTHON if exist "%COUNCIL_PYTHON%" set "PYEXE=%COUNCIL_PYTHON%"
if not defined PYEXE if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" set "PYEXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not defined PYEXE (
    for /f "usebackq delims=" %%i in (`conda info --base 2^>nul`) do (
        if not defined PYEXE if exist "%%i\envs\wizardCouncil\python.exe" set "PYEXE=%%i\envs\wizardCouncil\python.exe"
    )
)
if not defined PYEXE (
    for %%B in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\miniconda3" "%ProgramData%\Anaconda3" "C:\miniconda3" "C:\Anaconda3") do (
        if not defined PYEXE if exist "%%~B\envs\wizardCouncil\python.exe" set "PYEXE=%%~B\envs\wizardCouncil\python.exe"
    )
)
if not defined PYEXE (
    echo [run-windows] Could not find a Python environment.
    echo [run-windows] Run setup first:   setup.bat
    echo [run-windows] ...or point at one: set COUNCIL_PYTHON=C:\path\to\python.exe
    REM Keep the window open for a double-click launch, but never block a
    REM scripted --check / non-interactive run on a keypress.
    if not defined CHECK_ONLY pause
    exit /b 1
)
echo [run-windows] python: !PYEXE!

if defined CHECK_ONLY goto :do_check

REM ── Pick a GGUF model if user didn't set one ─────────────────
if not defined COUNCIL_GGUF_PATH (
    for %%F in ("%SCRIPT_DIR%models\*.gguf") do (
        if not defined COUNCIL_GGUF_PATH set "COUNCIL_GGUF_PATH=%%~fF"
    )
)
if not defined COUNCIL_GGUF_PATH (
    for %%F in ("%USERPROFILE%\models\*.gguf") do (
        if not defined COUNCIL_GGUF_PATH set "COUNCIL_GGUF_PATH=%%~fF"
    )
)

if defined COUNCIL_GGUF_PATH (
    echo [run-windows] model: !COUNCIL_GGUF_PATH!
) else (
    echo [run-windows] WARNING: no .gguf found in .\models\ or %%USERPROFILE%%\models\
    echo [run-windows] The app will open but the model won't load until you pick
    echo [run-windows] one via Browse in the UI.
)

REM ── Required env: backend selection ──────────────────────────
set "COUNCIL_BACKEND=gguf"

REM ── Sensible defaults (only if user hasn't set them) ─────────
if not defined COUNCIL_GGUF_GPU_LAYERS set "COUNCIL_GGUF_GPU_LAYERS=99"
if not defined COUNCIL_GGUF_N_CTX_DEBUG set "COUNCIL_GGUF_N_CTX_DEBUG=1"
REM On RTX 5080 (Blackwell sm_120) the cu124 torch wheel falls back to PTX
REM for sentence-transformers embeddings. Forcing embeddings to CPU avoids
REM rare JIT-compile stalls on dev boxes. On a real 4080 (sm_89, native)
REM you can comment this out for full-GPU embedding speed.
if not defined COUNCIL_EMBED_DEVICE set "COUNCIL_EMBED_DEVICE=cpu"

REM ── Tesseract OCR (P3b) ──────────────────────────────────────
REM If the UB-Mannheim Tesseract is installed in the default Program Files
REM location, expose it to the image parser (pytesseract reads this env via
REM the image parser in vault_index.py). Skip silently if not present —
REM image filename indexing still works without OCR.
if not defined COUNCIL_TESSERACT_CMD (
    if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
        set "COUNCIL_TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
)

echo [run-windows] GPU layers: %COUNCIL_GGUF_GPU_LAYERS%   backend: %COUNCIL_BACKEND%
REM Honest one-line GPU readiness heads-up (non-fatal; skip with COUNCIL_SKIP_GPU_CHECK=1)
if not defined COUNCIL_SKIP_GPU_CHECK "!PYEXE!" gpu_check.py --quiet
echo [run-windows] launching council_gui_engine.py ...

"!PYEXE!" council_gui_engine.py
set "EXIT=%ERRORLEVEL%"

if not "%EXIT%"=="0" (
    echo.
    echo [run-windows] Process exited with code %EXIT%.
    if not "%COUNCIL_GGUF_GPU_LAYERS%"=="0" (
        echo [run-windows] Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 (CPU only)...
        set "COUNCIL_GGUF_GPU_LAYERS=0"
        "!PYEXE!" council_gui_engine.py
        set "EXIT=!ERRORLEVEL!"
    )
)

endlocal & exit /b %EXIT%

REM ── --check: resolve + report GPU readiness, then exit (no launch) ──
:do_check
"!PYEXE!" gpu_check.py
exit /b %ERRORLEVEL%
