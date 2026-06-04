@echo off
REM ============================================================
REM  Council AI — Desktop Launch (legacy entry point)
REM
REM  This file used to start Ollama and a Pi cluster bridge. That
REM  backend was removed: Council now runs GGUF directly via
REM  llama-cpp-python (see council_engine.py, COUNCIL_BACKEND=gguf).
REM
REM  The real launcher is run-windows.bat. This file forwards to it
REM  so existing shortcuts keep working.
REM ============================================================
call "%~dp0run-windows.bat" %*
exit /b %ERRORLEVEL%
