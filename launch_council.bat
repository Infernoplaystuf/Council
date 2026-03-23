@echo off
REM ============================================================
REM  Council AI — Desktop Launch Script
REM  Hardware: RTX 5080 (16 GB VRAM) / 32 GB+ RAM
REM ============================================================
REM  OLLAMA_MAX_LOADED_MODELS=2 lets two 14B Q4 models coexist
REM  in 16 GB VRAM simultaneously (each ~9 GB), eliminating
REM  the model-swap pause between council roles.
REM ============================================================

set COUNCIL_PI_HOSTS=http://192.168.1.177:11434
set OLLAMA_MAX_LOADED_MODELS=2
set OLLAMA_NUM_PARALLEL=1
set OLLAMA_FLASH_ATTENTION=1

REM Start Ollama in background (skip if already running)
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo Starting Ollama...
    start "" /B ollama serve
    timeout /t 3 /nobreak >NUL
) else (
    echo Ollama already running.
)

REM Activate conda environment and launch council
call conda activate council
python council_gui_engine.py

pause
