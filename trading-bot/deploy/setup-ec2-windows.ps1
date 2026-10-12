#Requires -RunAsAdministrator
<#
.SYNOPSIS
  End-to-end setup of the trading bot on an AWS EC2 Windows Server instance.

.DESCRIPTION
  Run from the copied trading-bot folder (e.g. C:\trading-bot\deploy). Idempotent: safe to re-run.
    - Sets timezone to Eastern, keeps Windows Update from rebooting during market hours
    - Installs Python 3.11, creates .venv, installs requirements
    - Creates .env (generates WEBHOOK_SECRET), opens it in Notepad for Webull keys
    - Registers the bot as a boot-time scheduled task (NETWORK SERVICE, 127.0.0.1 only, auto-restart)
    - Registers a boot-time DuckDNS update task
    - Installs Caddy as a Windows service (HTTPS via Let's Encrypt, exposes only /webhook)
    - Opens Windows Firewall ports 80/443

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\trading-bot\deploy\setup-ec2-windows.ps1 -DuckDnsSubdomain yourbot

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\trading-bot\deploy\setup-ec2-windows.ps1 -Restart
  Restarts the bot only (after editing .env or updating app code).
#>
Param(
    [string]$DuckDnsSubdomain,
    [string]$DuckDnsToken,
    [string]$PythonVersion = "3.11.9",
    [int]$Port = 8000,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$BotDir   = Split-Path -Parent $PSScriptRoot
$LogDir   = Join-Path $BotDir "logs"
$EnvFile  = Join-Path $BotDir ".env"
$VenvPy   = Join-Path $BotDir ".venv\Scripts\python.exe"
$PyDir    = "C:\Python311"
$CaddyDir = "C:\caddy"
$OpsDir   = "C:\ProgramData\trading-bot-ops"
$BotTask  = "TradingBot"
$DnsTask  = "DuckDNS-Update"

# Well-known SIDs (locale independent)
$SID_ADMINS   = "*S-1-5-32-544"
$SID_SYSTEM   = "*S-1-5-18"
$SID_LOCALSVC = "*S-1-5-19"
$SID_NETSVC   = "*S-1-5-20"

function Step([string]$Msg) { Write-Host "`n==> $Msg" -ForegroundColor Cyan }

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Stop-Bot {
    Stop-ScheduledTask -TaskName $BotTask -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe' OR Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*uvicorn app.main:app*" -or $_.CommandLine -like "*run-bot.ps1*" } |
        ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
}

function Wait-BotHealthy {
    for ($i = 0; $i -lt 45; $i++) {
        try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 } catch { Start-Sleep -Seconds 2 }
    }
    Write-Warning "Bot did not answer /health. Last log lines:"
    Get-Content (Join-Path $LogDir "bot.log") -Tail 30 -ErrorAction SilentlyContinue
    throw "Bot is not healthy."
}

if (-not (Test-Path (Join-Path $BotDir "app\main.py"))) {
    throw "app\main.py not found under $BotDir. Copy the whole trading-bot folder (app, scripts, deploy, config.yaml, requirements.txt, .env.example) first."
}

if ($Restart) {
    Step "Restarting bot"
    Stop-Bot
    Start-ScheduledTask -TaskName $BotTask
    Wait-BotHealthy | Format-List
    return
}

# ---------------------------------------------------------------- inputs
if (-not $DuckDnsSubdomain) { $DuckDnsSubdomain = Read-Host "DuckDNS subdomain (e.g. yourbot for yourbot.duckdns.org)" }
$DuckDnsSubdomain = $DuckDnsSubdomain.Trim().ToLower() -replace '\.duckdns\.org$', ''
if ($DuckDnsSubdomain -notmatch '^[a-z0-9-]{1,63}$') { throw "Invalid DuckDNS subdomain '$DuckDnsSubdomain'." }

if (-not $DuckDnsToken) {
    $sec = Read-Host "DuckDNS token" -AsSecureString
    $DuckDnsToken = (New-Object System.Management.Automation.PSCredential("x", $sec)).GetNetworkCredential().Password
}
$DuckDnsToken = $DuckDnsToken.Trim()
if ($DuckDnsToken -notmatch '^[0-9a-fA-F-]{36}$') { throw "DuckDNS token should look like a GUID (36 chars)." }

$Domain = "$DuckDnsSubdomain.duckdns.org"

# ---------------------------------------------------------------- OS settings
Step "Timezone -> Eastern; Windows Update active hours 07:00-19:00 (no auto reboot while trading)"
Set-TimeZone -Id "Eastern Standard Time"
$wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
New-Item -Path $wu -Force | Out-Null
Set-ItemProperty -Path $wu -Name SetActiveHours   -Value 1  -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursStart -Value 7  -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursEnd   -Value 19 -Type DWord

# ---------------------------------------------------------------- Python
Step "Python $PythonVersion"
if (-not (Test-Path "$PyDir\python.exe")) {
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $installer
    $sig = Get-AuthenticodeSignature $installer
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "Python installer signature check failed ($($sig.Status))."
    }
    $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0", "Include_launcher=0", "TargetDir=$PyDir")
    if ($p.ExitCode -ne 0) { throw "Python installer exited with $($p.ExitCode)." }
    Remove-Item $installer -Force
}
Invoke-Native "$PyDir\python.exe" @("--version")

Step "Virtual env + requirements"
if (-not (Test-Path $VenvPy)) { Invoke-Native "$PyDir\python.exe" @("-m", "venv", (Join-Path $BotDir ".venv")) }
Invoke-Native $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", (Join-Path $BotDir "requirements.txt"))

# ---------------------------------------------------------------- .env
Step ".env"
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $BotDir ".env.example") $EnvFile }
$envText = [IO.File]::ReadAllText($EnvFile)
if ($envText -match '(?m)^WEBHOOK_SECRET=(CHANGE_ME.*)?\s*$') {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $envText = $envText -replace '(?m)^WEBHOOK_SECRET=[^\r\n]*', "WEBHOOK_SECRET=$secret"
    [IO.File]::WriteAllText($EnvFile, $envText)
    Write-Host "Generated a random WEBHOOK_SECRET in .env (use it in your TradingView alert JSON)."
}
if ($envText -match '(?m)^WEBULL_APP_KEY=(your_app_key)?\s*$') {
    Write-Host "Opening .env in Notepad: fill in Webull keys, keep DRY_RUN=true for now. Save and close to continue." -ForegroundColor Yellow
    Start-Process notepad.exe -ArgumentList "`"$EnvFile`"" -Wait
}

# ---------------------------------------------------------------- permissions
Step "Locking down $BotDir (Administrators, SYSTEM, NETWORK SERVICE only)"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Invoke-Native icacls.exe @($BotDir, "/inheritance:r", "/grant:r",
    "${SID_ADMINS}:(OI)(CI)F", "${SID_SYSTEM}:(OI)(CI)F", "${SID_NETSVC}:(OI)(CI)M", "/Q")

# ---------------------------------------------------------------- bot task
Step "Scheduled task '$BotTask' (starts at boot, restarts on crash)"
Stop-Bot
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $BotDir `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BotDir\deploy\run-bot.ps1`" -Port $Port"
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\NETWORK SERVICE" -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $BotTask -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $BotTask
$health = Wait-BotHealthy
Write-Host "Bot healthy: mode=$($health.mode) account=$($health.account_id)" -ForegroundColor Green

# ---------------------------------------------------------------- DuckDNS
Step "DuckDNS updater -> $Domain"
New-Item -ItemType Directory -Force -Path $OpsDir | Out-Null
Invoke-Native icacls.exe @($OpsDir, "/inheritance:r", "/grant:r", "${SID_ADMINS}:(OI)(CI)F", "${SID_SYSTEM}:(OI)(CI)F", "/Q")
$dnsScript = Join-Path $OpsDir "duckdns-update.ps1"
$dnsTemplate = @'
# Points __DOMAIN__ at this instance's current public IP. Runs at boot.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$log = Join-Path $PSScriptRoot "duckdns.log"
for ($i = 1; $i -le 12; $i++) {
    try {
        $r = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 -Uri "https://www.duckdns.org/update?domains=__SUB__&token=__TOKEN__&ip=").Content.Trim()
        Add-Content $log "$(Get-Date -Format s) attempt $i -> $r"
        if ($r -eq "OK") { exit 0 }
    } catch { Add-Content $log "$(Get-Date -Format s) attempt $i -> $($_.Exception.Message)" }
    Start-Sleep -Seconds 10
}
exit 1
'@
$dnsTemplate.Replace("__DOMAIN__", $Domain).Replace("__SUB__", $DuckDnsSubdomain).Replace("__TOKEN__", $DuckDnsToken) |
    Set-Content -Path $dnsScript -Encoding ASCII
$dnsAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dnsScript`""
Register-ScheduledTask -TaskName $DnsTask -Action $dnsAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal (New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)) -Force | Out-Null
& $dnsScript
if ($LASTEXITCODE -ne 0) { throw "DuckDNS update failed; see $OpsDir\duckdns.log (check subdomain/token)." }
Write-Host "DuckDNS updated." -ForegroundColor Green

# ---------------------------------------------------------------- Caddy
Step "Caddy (HTTPS reverse proxy, only /webhook is exposed)"
$caddyExe = Join-Path $CaddyDir "caddy.exe"
foreach ($d in @($CaddyDir, "$CaddyDir\data", "$CaddyDir\logs")) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
if (Get-Service caddy -ErrorAction SilentlyContinue) { Stop-Service caddy -Force }
if (-not (Test-Path $caddyExe)) {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/caddyserver/caddy/releases/latest"
    $ver = $rel.tag_name.TrimStart("v")
    $zipName = "caddy_${ver}_windows_amd64.zip"
    $base = "https://github.com/caddyserver/caddy/releases/download/v$ver"
    $zip = Join-Path $env:TEMP $zipName
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$zipName" -OutFile $zip
    $sums = (Invoke-WebRequest -UseBasicParsing -Uri "$base/caddy_${ver}_checksums.txt").Content
    $expected = ($sums -split "`n" | Where-Object { $_ -match "\s$([regex]::Escape($zipName))\s*$" } | Select-Object -First 1) -split '\s+' | Select-Object -First 1
    if (-not $expected -or (Get-FileHash $zip -Algorithm SHA512).Hash -ne $expected.ToUpper()) { throw "Caddy checksum mismatch." }
    $tmp = Join-Path $env:TEMP "caddy_extract"
    Expand-Archive $zip -DestinationPath $tmp -Force
    Copy-Item (Join-Path $tmp "caddy.exe") $caddyExe -Force
    Remove-Item $zip, $tmp -Recurse -Force
    Write-Host "Installed Caddy $ver"
}

$caddyfile = @"
{
	storage file_system {
		root C:/caddy/data
	}
	log {
		output file C:/caddy/logs/caddy.log
	}
}

$Domain {
	handle /webhook {
		reverse_proxy 127.0.0.1:$Port
	}
	handle {
		respond 404
	}
}
"@
Set-Content -Path "$CaddyDir\Caddyfile" -Value $caddyfile -Encoding ASCII
Invoke-Native $caddyExe @("fmt", "--overwrite", "$CaddyDir\Caddyfile")
Invoke-Native $caddyExe @("validate", "--config", "$CaddyDir\Caddyfile", "--adapter", "caddyfile")

Invoke-Native icacls.exe @($CaddyDir, "/inheritance:r", "/grant:r",
    "${SID_ADMINS}:(OI)(CI)F", "${SID_SYSTEM}:(OI)(CI)F", "${SID_LOCALSVC}:(OI)(CI)RX", "/Q")
foreach ($d in @("$CaddyDir\data", "$CaddyDir\logs")) {
    Invoke-Native icacls.exe @($d, "/grant", "${SID_LOCALSVC}:(OI)(CI)M", "/Q")
}

if (-not (Get-Service caddy -ErrorAction SilentlyContinue)) {
    New-Service -Name caddy -DisplayName "Caddy (trading-bot HTTPS)" -StartupType Automatic `
        -BinaryPathName "$caddyExe run --config $CaddyDir\Caddyfile --adapter caddyfile" | Out-Null
    $r = Get-CimInstance Win32_Service -Filter "Name='caddy'" |
        Invoke-CimMethod -MethodName Change -Arguments @{ StartName = "NT AUTHORITY\LocalService"; StartPassword = "" }
    if ($r.ReturnValue -ne 0) { throw "Could not set caddy service account (code $($r.ReturnValue))." }
    Invoke-Native sc.exe @("failure", "caddy", "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/5000")
}

Step "Windows Firewall: allow inbound 80/443"
foreach ($p in 80, 443) {
    $name = "TradingBot-Caddy-$p"
    Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -Name $name -DisplayName "Trading bot Caddy TCP $p" -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow | Out-Null
}
Start-Service caddy

Step "Waiting for Let's Encrypt certificate (up to 2 minutes)"
$certOk = $false
for ($i = 0; $i -lt 40 -and -not $certOk; $i++) {
    Start-Sleep -Seconds 3
    $certOk = [bool](Select-String -Path "$CaddyDir\logs\caddy.log" -Pattern "certificate obtained successfully|certificate is valid|skipping obtaining" -Quiet -ErrorAction SilentlyContinue)
    if (-not $certOk -and (Get-ChildItem "$CaddyDir\data\certificates" -Recurse -Filter "$Domain.crt" -ErrorAction SilentlyContinue)) { $certOk = $true }
}
if ($certOk) { Write-Host "Certificate OK for $Domain" -ForegroundColor Green }
else {
    Write-Warning "No certificate yet. Check the security group allows port 80 from 0.0.0.0/0 and that $Domain resolves to this instance. Log tail:"
    Get-Content "$CaddyDir\logs\caddy.log" -Tail 15 -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- summary
$publicIp = "unknown"
try {
    $tok = Invoke-RestMethod -Method PUT -Uri "http://169.254.169.254/latest/api/token" -Headers @{ "X-aws-ec2-metadata-token-ttl-seconds" = "60" } -TimeoutSec 3
    $publicIp = Invoke-RestMethod -Uri "http://169.254.169.254/latest/meta-data/public-ipv4" -Headers @{ "X-aws-ec2-metadata-token" = $tok } -TimeoutSec 3
} catch {}

Write-Host @"

=====================================================================
 Setup complete
   Public IP      : $publicIp
   Webhook URL    : https://$Domain/webhook
   Bot mode       : $($health.mode)
   Dashboard      : http://127.0.0.1:$Port  (open in a browser on this instance)
   Bot log        : $LogDir\bot.log
   Caddy log      : $CaddyDir\logs\caddy.log
   DuckDNS log    : $OpsDir\duckdns.log
   Restart bot    : powershell -ExecutionPolicy Bypass -File "$PSCommandPath" -Restart
 WEBHOOK_SECRET is in $EnvFile
=====================================================================
"@ -ForegroundColor Green
