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

REM ── venv check ───────────────────────────────────────────────
if not exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    echo [run-windows] .venv not found at %SCRIPT_DIR%.venv
    echo [run-windows] Run setup first: see BUILDING.md / installs.txt Section C
    pause
    exit /b 1
)

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
echo [run-windows] launching council_gui_engine.py ...

"%SCRIPT_DIR%.venv\Scripts\python.exe" council_gui_engine.py
set "EXIT=%ERRORLEVEL%"

if not "%EXIT%"=="0" (
    echo.
    echo [run-windows] Process exited with code %EXIT%.
    if not "%COUNCIL_GGUF_GPU_LAYERS%"=="0" (
        echo [run-windows] Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 (CPU only)...
        set "COUNCIL_GGUF_GPU_LAYERS=0"
        "%SCRIPT_DIR%.venv\Scripts\python.exe" council_gui_engine.py
        set "EXIT=!ERRORLEVEL!"
    )
)

endlocal & exit /b %EXIT%
