# The Model Arena's daily job (Windows Task Scheduler runs it at 2am).
# Starts Ollama if it is not up, runs the arena, and keeps a log per day.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs\arena"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs ("run-" + (Get-Date -Format "yyyy-MM-dd_HHmm") + ".log")

function Test-Ollama {
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/version" -TimeoutSec 5 | Out-Null; $true }
    catch { $false }
}

if (-not (Test-Ollama)) {
    "Ollama not running; starting 'ollama serve'" | Out-File -Append -Encoding utf8 $log
    Start-Process -WindowStyle Hidden -FilePath "ollama" -ArgumentList "serve"
    for ($i = 0; $i -lt 30 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 2 }
}

Set-Location $root
$env:PYTHONUNBUFFERED = "1"
& $py -m quant_duel.arena run *>> $log
exit $LASTEXITCODE
