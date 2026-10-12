# Install on AWS EC2 (Windows Server)

Runs during market hours only, HTTPS via a free DuckDNS hostname. One script does the full on-instance setup: [deploy/setup-ec2-windows.ps1](deploy/setup-ec2-windows.ps1).

```
TradingView --https://yourbot.duckdns.org/webhook--> Caddy (443, Windows service)
            --> uvicorn 127.0.0.1:8000 (scheduled task, NETWORK SERVICE) --> Webull
```

| Placeholder | Example |
|---|---|
| `yourbot` | your DuckDNS subdomain |
| `YOUR_TOKEN` | your DuckDNS token |
| `YOUR_WEBHOOK_SECRET` | `WEBHOOK_SECRET` in `C:\trading-bot\.env` (the script generates it) |
| `i-xxxxxxxx` | your instance id |

## Plan

1. Create a DuckDNS hostname.
2. Launch a Windows Server EC2 instance with a locked-down security group.
3. Zip the code on your PC, copy it over RDP.
4. Run `setup-ec2-windows.ps1` on the instance (Python, venv, `.env`, bot task, DuckDNS, Caddy + HTTPS, firewall).
5. Test the webhook from your PC, then point TradingView alerts at it.
6. Add EventBridge schedules to start/stop the instance around market hours.

---

## 1. Free hostname (duckdns.org)

1. Sign in at **duckdns.org**, create a subdomain, e.g. `yourbot` -> `yourbot.duckdns.org`.
2. Copy your **token**. Leave the IP empty; the instance updates it on every boot.

## 2. Launch the instance (EC2 -> Launch instance)

| Setting | Value |
|---|---|
| AMI | **Microsoft Windows Server 2022 Base** (64-bit x86) |
| Instance type | **t3.small** (2 GiB). t3.micro works but Windows is sluggish on 1 GiB |
| Key pair | RSA, `.pem` (needed to decrypt the Administrator password) |
| Network | Default VPC, **Auto-assign public IP: Enable** |
| Storage | 30 GB gp3 (Windows minimum) |

**Security group inbound rules:**

| Type | Port | Source |
|---|---|---|
| RDP | 3389 | My IP |
| HTTP | 80 | 0.0.0.0/0 (Let's Encrypt certificate challenge only; Caddy redirects to HTTPS) |
| HTTPS | 443 | `52.89.214.238/32`, `34.212.75.30/32`, `54.218.53.128/32`, `52.32.178.7/32` (TradingView) + **My IP** |

No Elastic IP needed (DuckDNS follows the IP). If Webull ever requires an IP allow-list for your API key, attach an Elastic IP instead.

## 3. Connect over RDP

1. EC2 -> select the instance -> **Connect** -> **RDP client** -> **Get password** -> upload the `.pem` -> **Decrypt password**.
2. **Download remote desktop file** and open it; log in as `Administrator`.

## 4. Copy the code (PowerShell on your PC)

```powershell
cd C:\Users\WA0G74\source\AI\mind-helper\trading-bot; Compress-Archive -Path app, scripts, deploy, config.yaml, requirements.txt, .env.example -DestinationPath (Join-Path ([Environment]::GetFolderPath('Desktop')) 'trading-bot.zip') -Force
```

Copy `trading-bot.zip` in Explorer on your PC (Ctrl+C) and paste it onto the instance's desktop inside the RDP window (Ctrl+V). `.env`, `state.json`, `.venv` and logs are intentionally not included.

## 5. Run the setup script (PowerShell **as Administrator** on the instance)

```powershell
Expand-Archive $env:USERPROFILE\Desktop\trading-bot.zip -DestinationPath C:\trading-bot -Force
powershell -ExecutionPolicy Bypass -File C:\trading-bot\deploy\setup-ec2-windows.ps1 -DuckDnsSubdomain yourbot
```

It prompts for the DuckDNS token (hidden input, kept out of shell history), then:

| Step | What it does |
|---|---|
| OS | Timezone -> Eastern; Windows Update active hours 07:00-19:00 so it never auto-reboots during trading |
| Python | Downloads Python 3.11.9 (signature verified), installs to `C:\Python311` |
| App | Creates `C:\trading-bot\.venv`, installs `requirements.txt` |
| `.env` | Creates it from `.env.example`, generates a random `WEBHOOK_SECRET`, opens Notepad for Webull keys. Keep `DRY_RUN=true`. Save and close Notepad to continue |
| Permissions | `C:\trading-bot` readable only by Administrators, SYSTEM, NETWORK SERVICE |
| Bot | Scheduled task **TradingBot**: starts at boot as NETWORK SERVICE, binds `127.0.0.1:8000`, `--workers 1`, restarts on crash, logs to `C:\trading-bot\logs\bot.log` |
| DuckDNS | Scheduled task **DuckDNS-Update** (SYSTEM, at boot); token stored in `C:\ProgramData\trading-bot-ops` (admin-only) |
| Caddy | Latest release from GitHub (SHA-512 verified) -> `C:\caddy`, Windows service **caddy** as LocalService, HTTPS for `yourbot.duckdns.org`, exposes only `/webhook` (everything else 404) |
| Firewall | Windows Firewall inbound 80/443 |

It ends with a summary (public IP, webhook URL, bot mode, log paths). Re-running it is safe.

`--workers 1` is required: state is in memory, more workers = duplicate orders.

## 6. Test from your PC

```powershell
Invoke-RestMethod -Method Post -Uri https://yourbot.duckdns.org/webhook -ContentType application/json `
  -Body '{"secret":"YOUR_WEBHOOK_SECRET","ticker":"SPY","action":"CALL","signal":"test","price":500}'
```

Any JSON reply with `accepted` true or false means the path works end to end. Outside 09:30-16:00 ET expect `accepted: false` with an RTH reason.

## 7. TradingView alerts

Follow [TV setup.md](TV%20setup.md) steps 4-7, using `https://yourbot.duckdns.org/webhook` as the Webhook URL (it no longer changes). The alert JSON must include `"secret":"YOUR_WEBHOOK_SECRET"`.

## 8. Dashboard (private)

The dashboard is not exposed publicly. In the RDP session open Edge -> **http://127.0.0.1:8000**.

Quick check from PowerShell on the instance: `Invoke-RestMethod http://127.0.0.1:8000/health`

## 9. Run only during market hours (EventBridge -> Scheduler -> Create schedule)

Windows boots slower than Linux, so start 15 minutes before the open.

| | Start | Stop |
|---|---|---|
| Type | Recurring, cron-based | Recurring, cron-based |
| Cron | `45 8 ? * MON-FRI *` | `30 16 ? * MON-FRI *` |
| Time zone | America/New_York | America/New_York |
| Flexible time window | Off | Off |
| Target | All APIs -> **EC2 -> StartInstances** | All APIs -> **EC2 -> StopInstances** |
| Input | `{"InstanceIds":["i-xxxxxxxx"]}` | `{"InstanceIds":["i-xxxxxxxx"]}` |
| Permissions | Create new role for this schedule | Create new role for this schedule |

DuckDNS, the bot and Caddy all start automatically on boot (no login needed).

---

## Day-to-day (PowerShell as Administrator on the instance)

| Task | Command |
|---|---|
| Live bot log | `Get-Content C:\trading-bot\logs\bot.log -Tail 50 -Wait` |
| Restart bot after editing `.env` | `powershell -ExecutionPolicy Bypass -File C:\trading-bot\deploy\setup-ec2-windows.ps1 -Restart` |
| Tune `config.yaml` | Edit `C:\trading-bot\config.yaml`; reloaded on every alert, no restart needed |
| Status | `Get-ScheduledTask TradingBot, DuckDNS-Update \| Select TaskName, State; Get-Service caddy` |
| Caddy log | `Get-Content C:\caddy\logs\caddy.log -Tail 30` |
| DuckDNS log | `Get-Content C:\ProgramData\trading-bot-ops\duckdns.log -Tail 10` |
| Windows updates | Once a month after 16:00: Settings -> Windows Update -> Install, then reboot |

**Update the code:** re-run step 4 on your PC, paste the zip, then on the instance:

```powershell
Expand-Archive $env:USERPROFILE\Desktop\trading-bot.zip -DestinationPath C:\trading-bot -Force
C:\trading-bot\.venv\Scripts\python.exe -m pip install -q -r C:\trading-bot\requirements.txt
powershell -ExecutionPolicy Bypass -File C:\trading-bot\deploy\setup-ec2-windows.ps1 -Restart
```

`.env` and `state.json` are not in the zip, so they are preserved.

Keep `DRY_RUN=true` until TradingView alerts show correctly in the dashboard.

## Troubleshooting

| Symptom | Check |
|---|---|
| Script says bot not healthy | `C:\trading-bot\logs\bot.log` (usually a bad `.env` value or missing `WEBULL_ACCOUNT_ID` with `DRY_RUN=false`) |
| No certificate | Security group must allow port 80 from `0.0.0.0/0`; `Resolve-DnsName yourbot.duckdns.org` must return the instance public IP |
| Test from PC times out | Your current IP must be in the 443 rule ("My IP" changes on home networks) |
| DuckDNS update fails | Wrong subdomain/token; see `duckdns.log` |

## Cost (approximate, us-east-1; check the AWS Pricing Calculator)

| | Market hours (~175 h/month) | 24/7 |
|---|---|---|
| t3.small Windows (license included) | ~$7 | ~$30 |
| 30 GB gp3 | ~$2.40 | ~$2.40 |
| Public IPv4 | ~$0.90 | ~$3.60 |
| **Total** | **~$10/month** | **~$36/month** |

The Linux setup in [install-instruction.md](install-instruction.md) is ~$3/month if Windows isn't required.
