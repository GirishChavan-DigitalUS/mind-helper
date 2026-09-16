# Run the auto-trader locally. Requires .env (copy from .env.example) and a venv.
#
# First-time setup:
#   python -m venv .venv
#   .\.venv\Scripts\Activate.ps1
#   pip install -r requirements.txt
#   Copy-Item .env.example .env   # then edit .env
#
# Start the API (from the trading-bot folder):
Param(
    [switch]$Smoke,
    [switch]$Test
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if ($Smoke) {
    python -m scripts.smoke_test SPY 500
    return
}
if ($Test) {
    python -m scripts.dry_run_test
    return
}

# Load HOST/PORT from .env if present (uvicorn also reads .env via app.config)
$env:PYTHONUNBUFFERED = "1"
python -m uvicorn app.main:app --host $(if ($env:HOST) { $env:HOST } else { "127.0.0.1" }) --port $(if ($env:PORT) { $env:PORT } else { "8000" })

# In a SECOND terminal, expose the webhook publicly with Cloudflare Tunnel:
#   cloudflared tunnel --url http://127.0.0.1:8000
# Then point your TradingView alert webhook at  https://<tunnel-host>/webhook
