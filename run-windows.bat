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
REM        - else first .gguf in .\models\ or %USERPROFILE%\models\,
REM          marked COUNCIL_GGUF_PATH_AUTO=1 so the model saved in the app wins
REM    4. Exports sensible Windows defaults (GPU offload, n_ctx debug)
REM    5. Launches the GUI — the Qt app (council_qt.py) by default; the
REM       classic Tk app (council_gui_engine.py) with --tk or COUNCIL_UI=tk,
REM       or automatically when PySide6 is not installed.
REM
REM  Usage:
REM    run-windows.bat
REM    run-windows.bat --tk                     :: the classic Tk UI
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
if /i "%~1"=="--tk" set "COUNCIL_UI=tk"

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
REM COUNCIL_GGUF_PATH_AUTO=1 tells the app the path below is OUR guess, not the
REM user's choice. Without it the guess beat the main model saved in the app on
REM every launch - measured 2026-09-29: always granite-3.1-8b from ~\models,
REM whatever was picked. The app now starts on the saved model when AUTO=1
REM and that file is on disk - onboarding.apply_saved_gguf_path.
REM Cleared first: only this block sets it, so an inherited value can never
REM turn a path the user set into a guess.
REM The search runs with delayed expansion OFF. With it on, a ! in a found
REM name was dropped - measured 2026-09-29: wow!model.gguf was exported as
REM wowmodel.gguf, a file that does not exist. The name still crosses the
REM endlocal through one delayed-expansion pass, which drops a lone ! and, on
REM a line that has a !, eats every caret - so a name with a ! goes out with
REM each caret doubled and each ! escaped, and any other name goes out as is.
REM The escaping is jumped over when nothing was found: a %%VAR:x=y%% edit of
REM an UNDEFINED variable breaks the line it is on - measured 2026-09-29, cmd
REM stopped with "The syntax of the command is incorrect." even behind an
REM `if defined` guard, because the edit is made before the IF runs.
set "COUNCIL_GGUF_PATH_AUTO="
setlocal disabledelayedexpansion
set "GGUF_PICK="
if not defined COUNCIL_GGUF_PATH for %%F in ("%SCRIPT_DIR%models\*.gguf") do if not defined GGUF_PICK set "GGUF_PICK=%%~fF"
if not defined COUNCIL_GGUF_PATH if not defined GGUF_PICK for %%F in ("%USERPROFILE%\models\*.gguf") do if not defined GGUF_PICK set "GGUF_PICK=%%~fF"
if not defined GGUF_PICK goto :gguf_pick_out
if not "%GGUF_PICK%"=="%GGUF_PICK:!=%" set "GGUF_PICK=%GGUF_PICK:^=^^%"
set "GGUF_PICK=%GGUF_PICK:!=^!%"
:gguf_pick_out
endlocal & if not "%GGUF_PICK%"=="" set "COUNCIL_GGUF_PATH=%GGUF_PICK%" & set "COUNCIL_GGUF_PATH_AUTO=1"

if defined COUNCIL_GGUF_PATH (
    echo [run-windows] model: !COUNCIL_GGUF_PATH!
) else (
    echo [run-windows] WARNING: no .gguf found in .\models\ or %%USERPROFILE%%\models\
    echo [run-windows] The app will open but the model won't load until you pick
    echo [run-windows] one via Browse in the UI.
)
if defined COUNCIL_GGUF_PATH_AUTO echo [run-windows] that is the first .gguf found - a main model saved in the app is used instead while it is on disk.

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

REM ── Which UI: Qt by default, Tk on request or when Qt is missing ──
set "COUNCIL_ENTRY=council_qt.py"
if /i "%COUNCIL_UI%"=="tk" set "COUNCIL_ENTRY=council_gui_engine.py"
if "!COUNCIL_ENTRY!"=="council_qt.py" (
    "!PYEXE!" -c "import PySide6" >nul 2>&1
    if errorlevel 1 (
        echo [run-windows] PySide6 is not installed in this environment, so the classic Tk UI is starting.
        echo [run-windows] For the new UI:  "!PYEXE!" -m pip install PySide6
        set "COUNCIL_ENTRY=council_gui_engine.py"
    )
)
echo [run-windows] launching !COUNCIL_ENTRY! ...

"!PYEXE!" !COUNCIL_ENTRY!
set "EXIT=%ERRORLEVEL%"

REM The parentheses in the Retrying echo are escaped with ^ because an
REM unescaped ) inside a ( ) block closes it. Measured 2026-09-29 with a
REM stub entry: unescaped, cmd rejected this whole block with "... was
REM unexpected at this time." after EVERY run, clean or crashed - the
REM launcher exited 255 and the CPU retry below never ran.
REM
REM The retry is for a NATIVE crash only - the GPU path failing: a CUDA
REM wheel / driver mismatch, VRAM running out mid-load - as run-linux.sh and
REM run-wsl.sh retry only on signals 132-139. On Windows a native crash exits
REM with an NTSTATUS error code of facility 0, 0xC0000000-0xC000FFFF, which
REM ERRORLEVEL shows as -1073741824 to -1073676289:
REM   -1073741819  0xC0000005  access violation
REM   -1073741795  0xC000001D  illegal instruction - a wheel built for another CPU
REM   -1073740791  0xC0000409  fast fail - how the C runtime's abort usually ends
REM   -1073741571  0xC00000FD  stack overflow
REM and an abort / GGML_ABORT that does not fast-fail exits with 3. Nothing
REM else is retried: any other exit is the app's or the user's, and retrying
REM it reopened an app the user had just closed. Measured 2026-09-29: a
REM Python exception at startup exits 1, taskkill /F exits 1, PowerShell's
REM Stop-Process exits -1 0xFFFFFFFF - negative, so "any negative exit" had
REM the app reopen on the CPU - and Ctrl+C, an unhandled KeyboardInterrupt,
REM exits -1073741510 0xC000013A: in the range but no crash, so it is
REM excluded by value.
REM
REM And only while a GPU load is unconfirmed. The engine writes .gpu_attempt
REM in the vault before it loads a model onto the GPU, and deletes it after
REM the first answer and at a clean close - council_engine.py
REM _gpu_mark_attempt / _gpu_confirm_success, council_core\shutdown.py
REM close_session. A native crash with no sentinel left - say while closing,
REM after the model had answered - is not the GPU failing, and retrying it
REM reopened the app the user had just closed.
REM The engine puts it in COUNCIL_VAULT_ROOT, else in USERPROFILE\.council\vault
REM - council_engine._gpu_sentinel_path, which, unlike council_core.paths
REM vault_dir, ignores COUNCIL_APP_DIR - so vault_dir's COUNCIL_APP_DIR\vault
REM is looked in as well. No vault to look in, no retry. The !VAR! form reads
REM a folder name with ! ^ & ) in it as it is.
set "CRASHED="
if !EXIT! GEQ -1073741824 if !EXIT! LEQ -1073676289 if not "!EXIT!"=="-1073741510" set "CRASHED=1"
if "!EXIT!"=="3" set "CRASHED=1"
set "GPU_PENDING="
if defined COUNCIL_VAULT_ROOT (
    if exist "!COUNCIL_VAULT_ROOT!\.gpu_attempt" set "GPU_PENDING=1"
) else (
    if defined USERPROFILE if exist "!USERPROFILE!\.council\vault\.gpu_attempt" set "GPU_PENDING=1"
    if defined COUNCIL_APP_DIR if exist "!COUNCIL_APP_DIR!\vault\.gpu_attempt" set "GPU_PENDING=1"
)
set "RETRY="
if defined CRASHED if defined GPU_PENDING if not "!COUNCIL_GGUF_GPU_LAYERS!"=="0" set "RETRY=1"
if not "%EXIT%"=="0" (
    echo.
    echo [run-windows] Process exited with code %EXIT%.
    if defined RETRY (
        echo [run-windows] That is a native crash while a GPU load was unconfirmed - most often a CUDA wheel / driver mismatch, or VRAM running out.
        echo [run-windows] Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 ^(CPU only^)...
        set "COUNCIL_GGUF_GPU_LAYERS=0"
        "!PYEXE!" !COUNCIL_ENTRY!
        set "EXIT=!ERRORLEVEL!"
    )
    if defined CRASHED if not defined GPU_PENDING echo [run-windows] That is a native crash, but no GPU load was waiting to be confirmed, so the app is not reopened.
)

endlocal & exit /b %EXIT%

REM ── --check: resolve + report GPU readiness, then exit (no launch) ──
:do_check
"!PYEXE!" gpu_check.py
exit /b %ERRORLEVEL%
