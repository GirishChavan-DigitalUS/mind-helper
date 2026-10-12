# TradingView Alert -> Webhook Setup

The app runs on your PC at `127.0.0.1:8000`, which TradingView can't reach. A free Cloudflare quick tunnel gives it a public HTTPS URL.

```
TradingView alert --https://xxxx.trycloudflare.com/webhook--> Cloudflare Tunnel --> Your PC 127.0.0.1:8000 (shadow mode)
```

---

## Step 1: Start the app (terminal 1)

```powershell
cd C:\Users\WA0G74\source\AI\mind-helper\trading-bot
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check the log shows `started (mode=shadow ...)`. Leave this window open.

## Step 2: Create the public URL (terminal 2)

```powershell
winget install --id Cloudflare.cloudflared
# close and reopen the terminal so the new command is found, then:
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the URL it prints, e.g. `https://random-words.trycloudflare.com`. Leave this window open.

- The URL **changes every time cloudflared restarts**, so the alert's webhook URL must be updated each time. Fine for testing; use the EC2 setup ([install-instruction.md](install-instruction.md)) for a permanent URL.
- While the tunnel runs, anyone with the URL can open the dashboard. Stop the tunnel when done.

## Step 3: Test the tunnel (terminal 3)

Copy `WEBHOOK_SECRET` from `.env`, then:

```powershell
Invoke-RestMethod -Method Post -Uri https://random-words.trycloudflare.com/webhook -ContentType application/json `
  -Body '{"secret":"YOUR_WEBHOOK_SECRET","ticker":"SPY","action":"CALL","signal":"TUNNEL_TEST","price":500}'
```

Any JSON reply means the path works. Outside market hours expect `accepted: false` with a regular-trading-hours (RTH) reason.

## Step 4: TradingView requirements

- A **paid plan** (Essential or higher). Free accounts can't send webhooks.
- **2FA turned on**: Profile -> Settings -> Security -> Two-factor authentication.

## Step 5: Create the CALL (bullish) alert

1. Open a chart of a symbol in your universe, e.g. **SPY**, **5-minute** timeframe.
2. Click **Alert** (clock icon) or press `Alt + A`.
3. **Settings** tab:
   - **Condition**: your indicator's bullish signal. For a quick test: `SPY` -> `Crossing` -> a price near the current price.
   - **Trigger**: **Once per bar close** (avoids signals that disappear before the bar closes).
   - **Expiration**: Open-ended, or the longest your plan allows.
   - **Message**: delete the default text and paste:
     ```json
     {"secret":"YOUR_WEBHOOK_SECRET","ticker":"{{ticker}}","action":"CALL","signal":"BULL_5M","price":{{close}},"tf":"{{interval}}"}
     ```
4. **Notifications** tab: tick **Webhook URL** and paste `https://random-words.trycloudflare.com/webhook`.
5. Click **Create**.

## Step 6: Create the PUT (bearish) alert

Repeat Step 5 with your bearish signal as the condition and this message:

```json
{"secret":"YOUR_WEBHOOK_SECRET","ticker":"{{ticker}}","action":"PUT","signal":"BEAR_5M","price":{{close}},"tf":"{{interval}}"}
```

## Step 7: Check that it works

When an alert fires:

- **Terminal 1** shows `POST /webhook 200` and a line starting `ENTER` or `REJECT`.
- **Dashboard** (http://127.0.0.1:8000): the alert appears in the Alerts list with accepted/rejected and the reason.
- **TradingView**: Alerts panel -> **Log** tab shows the alert fired.

---

## Message rules

| Field | Required | Notes |
|---|---|---|
| `secret` | Yes | Must exactly match `WEBHOOK_SECRET` in `.env` |
| `ticker` | Yes | `{{ticker}}`; symbol must be in the `universe` list in `config.yaml` |
| `action` | Yes | `CALL` or `PUT` only |
| `price` | Yes | `{{close}}`, no quotes |
| `signal` | No | Any label; shown on the dashboard and used to drop duplicates |
| `tf` | No | `{{interval}}` |

- Don't include `"time":"{{time}}"`. TradingView sends it as text; the app expects a number.
- If your Pine indicator sends its own `alert()` JSON, choose **"Any alert() function call"** as the condition. That JSON must include the same fields, including `secret`.
- Outside 09:30-16:00 ET, alerts are recorded but rejected by the RTH rule. Run the real test during market hours.
