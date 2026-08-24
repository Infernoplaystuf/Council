@echo off
REM ============================================================
REM  start_comfyui.bat - local image backend for Anvil
REM ============================================================
REM  Starts ComfyUI on http://localhost:8188, which Anvil's Pixel
REM  Art tab detects for REFERENCE image generation (Inspiration
REM  -> "Reference (local model)").
REM
REM  Reference images are for looking at while you paint. They are
REM  written to vault\art_reference\ and are never loaded into the
REM  canvas or into a game project - see art_reference.py.
REM
REM  Everything runs on this machine; nothing is uploaded.
REM
REM  Leave this window open while you use the feature. Ctrl+C stops it.
REM ============================================================

set COMFY_DIR=%USERPROFILE%\ComfyUI

if not exist "%COMFY_DIR%\main.py" (
    echo.
    echo ComfyUI not found at "%COMFY_DIR%".
    echo.
    echo Install it with:
    echo     git clone https://github.com/comfyanonymous/ComfyUI.git "%COMFY_DIR%"
    echo     pip install -r "%COMFY_DIR%\requirements.txt"
    echo.
    echo Then drop a .safetensors checkpoint into:
    echo     %COMFY_DIR%\models\checkpoints\
    echo.
    pause
    exit /b 1
)

echo Starting ComfyUI on http://localhost:8188 ...
echo (Leave this window open. Ctrl+C to stop.)
echo.
cd /d "%COMFY_DIR%"
python main.py --port 8188
