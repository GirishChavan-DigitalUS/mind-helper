# Install on AWS EC2 (t4g.micro, Amazon Linux 2023)

Runs during market hours only, HTTPS via a free DuckDNS hostname. ~$3/month.

```
TradingView --https://yourbot.duckdns.org/webhook--> Caddy (443) --> uvicorn 127.0.0.1:8000 --> Webull
```

Replace these placeholders everywhere:

| Placeholder | Example |
|---|---|
| `yourbot` | your DuckDNS subdomain |
| `YOUR_TOKEN` | your DuckDNS token |
| `YOUR_WEBHOOK_SECRET` | value of `WEBHOOK_SECRET` in `.env` |
| `<PUBLIC_IP>` | instance public IPv4 (changes on each start) |
| `C:\Users\WA0G74\Downloads\TVBot-KP.pem` | your EC2 key pair file |

---

## 1. Free hostname (duckdns.org)

1. Sign in at **duckdns.org**.
2. Create a subdomain, e.g. `yourbot` -> `yourbot.duckdns.org`.
3. Copy your **token**. Leave the IP empty; the instance updates it on boot.

## 2. Launch the instance (EC2 -> Launch instance)

| Setting | Value |
|---|---|
| AMI | **Amazon Linux 2023**, Architecture **64-bit (Arm)** |
| Instance type | **t4g.micro** |
| Key pair | RSA, `.pem` |
| Network | Default VPC, **Auto-assign public IP: Enable** |
| Storage | 8 GB gp3 (default) |

**Security group inbound rules:**

| Type | Port | Source |
|---|---|---|
| SSH | 22 | My IP |
| HTTP | 80 | 0.0.0.0/0 (Let's Encrypt certificate only) |
| HTTPS | 443 | `52.89.214.238/32`, `34.212.75.30/32`, `54.218.53.128/32`, `52.32.178.7/32` (TradingView) + **My IP** |

No Elastic IP needed.

## 3. Copy the code (PowerShell on your PC)

```powershell
icacls C:\Users\WA0G74\Downloads\TVBot-KP.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i C:\Users\WA0G74\Downloads\TVBot-KP.pem ec2-user@<PUBLIC_IP> "mkdir -p ~/trading-bot"
cd C:\Users\WA0G74\source\AI\mind-helper\trading-bot
scp -i C:\Users\WA0G74\Downloads\TVBot-KP.pem -r app scripts config.yaml requirements.txt .env.example ec2-user@<PUBLIC_IP>:~/trading-bot/
ssh -i C:\Users\WA0G74\Downloads\TVBot-KP.pem ec2-user@<PUBLIC_IP>
```

The `icacls` line fixes Windows SSH's "UNPROTECTED PRIVATE KEY FILE" error.

## 4. Install the app (on the instance)

```bash
sudo dnf install -y python3.11 python3.11-pip nano
sudo timedatectl set-timezone America/New_York

cd ~/trading-bot
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env     # Webull keys, WEBHOOK_SECRET; keep HOST=127.0.0.1, DRY_RUN=true
chmod 600 .env
```

## 5. Run the app as a service

```bash
sudo tee /etc/systemd/system/trading-bot.service > /dev/null <<'EOF'
[Unit]
Description=TradingView to Webull bot
After=network-online.target
Wants=network-online.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/trading-bot
ExecStart=/home/ec2-user/trading-bot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
sudo journalctl -u trading-bot -n 30      # expect "started (mode=dry-run ...)"
```

`--workers 1` is required: state is in memory, more workers = duplicate orders.

## 6. Update DuckDNS on every boot

```bash
echo 'DUCKDNS_URL=https://www.duckdns.org/update?domains=yourbot&token=YOUR_TOKEN&ip=' | sudo tee /etc/duckdns.env > /dev/null
sudo chmod 600 /etc/duckdns.env

sudo tee /etc/systemd/system/duckdns.service > /dev/null <<'EOF'
[Unit]
Description=Point DuckDNS at this instance's public IP
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/duckdns.env
ExecStart=/usr/bin/curl -fsS --retry 5 ${DUCKDNS_URL}

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now duckdns
sudo journalctl -u duckdns -n 5           # expect "OK"
```

## 7. Install Caddy (HTTPS, exposes only /webhook)

```bash
sudo curl -fsSL -o /usr/local/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=arm64"
sudo chmod +x /usr/local/bin/caddy
sudo mkdir -p /etc/caddy

sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
yourbot.duckdns.org {
	handle /webhook {
		reverse_proxy 127.0.0.1:8000
	}
	handle {
		respond 404
	}
}
EOF

sudo tee /etc/systemd/system/caddy.service > /dev/null <<'EOF'
[Unit]
Description=Caddy
After=network-online.target duckdns.service
Wants=network-online.target

[Service]
User=ec2-user
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo journalctl -u caddy -f               # wait for "certificate obtained successfully", then Ctrl+C
```

## 8. Test from your PC

```powershell
Invoke-RestMethod -Method Post -Uri https://yourbot.duckdns.org/webhook -ContentType application/json `
  -Body '{"secret":"YOUR_WEBHOOK_SECRET","ticker":"SPY","action":"CALL","signal":"test","price":500}'
```

Any JSON reply with `accepted` true or false means the path works end to end.

## 9. TradingView alerts

1. Create the alert -> condition **"Any alert() function call"**.
2. **Notifications** tab -> tick **Webhook URL** -> `https://yourbot.duckdns.org/webhook`.
3. The alert JSON must include `"secret":"YOUR_WEBHOOK_SECRET"`.

Requires a paid TradingView plan with 2FA enabled. TradingView cancels requests taking longer than ~3 seconds.

## 10. Dashboard (private, via SSH tunnel)

```powershell
ssh -i C:\Users\WA0G74\Downloads\TVBot-KP.pem -L 8000:127.0.0.1:8000 ec2-user@yourbot.duckdns.org
```

Keep the window open and browse to **http://localhost:8000**.

## 11. Run only during market hours (EventBridge -> Scheduler -> Create schedule)

| | Start | Stop |
|---|---|---|
| Type | Recurring, cron-based | Recurring, cron-based |
| Cron | `0 9 ? * MON-FRI *` | `30 16 ? * MON-FRI *` |
| Time zone | America/New_York | America/New_York |
| Flexible time window | Off | Off |
| Target | All APIs -> **EC2 -> StartInstances** | All APIs -> **EC2 -> StopInstances** |
| Input | `{"InstanceIds":["i-xxxxxxxx"]}` | `{"InstanceIds":["i-xxxxxxxx"]}` |
| Permissions | Create new role for this schedule | Create new role for this schedule |

DuckDNS, the bot, and Caddy all start automatically on boot.

---

## Day-to-day

| Task | Command (on the instance) |
|---|---|
| Live logs | `sudo journalctl -u trading-bot -f` |
| Restart after editing `.env` | `sudo systemctl restart trading-bot` |
| Tune `config.yaml` | Edit it; reloaded on every alert, no restart needed |
| Status of all services | `systemctl status trading-bot caddy duckdns` |

Keep `DRY_RUN=true` until TradingView alerts show correctly in the dashboard.

## Cost

~$3/month on the market-hours schedule (compute ~$1.50, disk ~$0.65, public IPv4 ~$0.90). ~$10/month if run 24/7.
