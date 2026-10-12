# Keeps the bot running. Launched at boot by the "TradingBot" scheduled task.
Param([int]$Port = 8000)

$BotDir = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $BotDir "logs"
$Log = Join-Path $LogDir "bot.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $BotDir
[Environment]::CurrentDirectory = $BotDir
$env:PYTHONUNBUFFERED = "1"

while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 20MB) {
        Move-Item $Log "$Log.$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
    }
    Add-Content $Log "$(Get-Date -Format s) run-bot: starting uvicorn on 127.0.0.1:$Port"
    # --workers 1 is required: state is in memory, more workers = duplicate orders.
    cmd.exe /c ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1 --proxy-headers >> logs\bot.log 2>&1"
    Add-Content $Log "$(Get-Date -Format s) run-bot: uvicorn exited with code $LASTEXITCODE; restarting in 5s"
    Start-Sleep -Seconds 5
}
