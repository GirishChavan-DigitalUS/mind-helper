#!/usr/bin/env python3
"""
iPhone Store Availability Watcher
----------------------------------
Polls Apple's own store-pickup API (the same one apple.com's "Check Availability"
modal calls) on a schedule and pushes a notification the moment any store near
you has the model/finish/capacity in stock for pickup.

WHY THIS WORKS
Apple's site calls a public JSON endpoint to populate the availability modal:

    https://www.apple.com/{country}/shop/fulfillment-messages
        ?pl=true
        &parts.0={PART_NUMBER}
        &location={ZIP}

No login/API key needed. This has been used by trading-style "restock watchers"
for over a decade (the pattern predates the iPhone X). It's read-only, low
frequency, and hits the same endpoint your browser already hits.

SETUP (2 minutes)
1. pip install requests apscheduler
2. Get the PART NUMBER for the exact model/finish/capacity you want:
   - Open the iPhone buy page on apple.com, pick your options.
   - Open browser DevTools -> Network tab -> filter "fulfillment-messages"
     -> click "Check availability" on the page.
   - Look at the request URL/query params: parts.0=MXXXXX/A  <- that's it.
   - (Alternative: the "Continue"/order URL near checkout often embeds it too.)
3. Fill in CONFIG below.
4. Run: python iphone_watcher.py
   It checks immediately, then every CHECK_INTERVAL_SECONDS, forever.

NOTIFICATIONS
Default is ntfy.sh - free, no signup, no API key. Install the "ntfy" app
(iOS/Android) or just watch https://ntfy.sh/<your-topic> in a browser tab,
subscribe to the topic name you set below, and you'll get a push the instant
stock appears.
"""

import logging
from datetime import datetime

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# ============================== CONFIG ======================================

ZIP_CODE = "77479"          # City or ZIP, same as the modal's "City or Zip" box
COUNTRY = "us"               # apple.com/<country>/shop/...  ("us", "uk" -> "uk", etc.)

# Friendly name -> Apple part number (SKU). Add as many as you want to track.
PRODUCTS = {
    "iPhone 18 Pro Max 256GB Burgundy": "MJW64LL/A",   # <-- replace with real part number
    "iPhone 18 Pro Max 256GB Silver": "MJW74LL/A",
}

CHECK_INTERVAL_SECONDS = 120   # be polite - 60-180s is plenty, don't hammer it

# Notification method: "ntfy", "console", or "email"
NOTIFY_METHOD = "ntfy"
NTFY_TOPIC = "girish-iphone-watch-8341"   # make this unique/hard to guess

# Only used if NOTIFY_METHOD == "email"
EMAIL_FROM = "you@gmail.com"
EMAIL_TO = "you@gmail.com"
EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail App Password, not your real password
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iphone_watcher")

FULFILLMENT_URL = "https://www.apple.com/{country}/shop/retail/pickup-message"

# Remembers which (product, store) pairs we've already alerted on, so you get
# ONE ping when it flips available, not one every poll.
_already_notified: set[tuple[str, str]] = set()


def check_product(name: str, part_number: str) -> list[dict]:
    """Return a list of {store, city, message} dicts for stores with pickup stock."""
    params = {
        "pl": "true",
        "parts.0": part_number,
        "location": ZIP_CODE,
    }
    url = FULFILLMENT_URL.format(country=COUNTRY)

    resp = requests.get(url, params=params, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (compatible; personal-availability-check/1.0)"
    })
    resp.raise_for_status()
    data = resp.json()

    stores = data.get("body", {}).get("stores", [])
    available = []

    for store in stores:
        parts = store.get("partsAvailability", {})
        info = parts.get(part_number)
        if not info:
            continue
        pickup_display = info.get("pickupDisplay", "")  # "available" | "unavailable" | ...
        if pickup_display == "available":
            available.append({
                "store": store.get("storeName", "Unknown store"),
                "city": store.get("city", ""),
                "message": info.get("pickupSearchQuote", ""),
            })

    return available


def notify(subject: str, body: str, priority: str = "urgent",
           links: list[tuple[str, str]] | None = None) -> None:
    """links: (label, url) pairs shown as ntfy buttons; ntfy allows at most 3."""
    if NOTIFY_METHOD == "console":
        log.info(f"🔔 {subject}\n{body}")

    elif NOTIFY_METHOD == "ntfy":
        headers = {"Title": subject, "Priority": priority, "Tags": "package,iphone"}
        if links:
            headers["Click"] = links[0][1]
            # ntfy action syntax uses ',' and ';' as separators, so strip them from labels.
            headers["Actions"] = "; ".join(
                f"view, {label.replace(',', ' ').replace(';', ' ')}, {url}" for label, url in links[:3]
            )
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning(f"ntfy push failed, falling back to console: {e}")
            log.info(f"🔔 {subject}\n{body}")

    elif NOTIFY_METHOD == "email":
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            log.warning(f"email send failed, falling back to console: {e}")
            log.info(f"🔔 {subject}\n{body}")


def run_check() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name, part_number in PRODUCTS.items():
        try:
            stores = check_product(name, part_number)
        except Exception as e:  # noqa: BLE001 - one bad product must not stop the loop
            log.error(f"[{ts}] {name}: check failed ({e})")
            continue

        if not stores:
            log.info(f"[{ts}] {name}: no pickup stock near {ZIP_CODE}")
            continue

        new_hits = [s for s in stores if (name, s["store"]) not in _already_notified]

        if new_hits:
            lines = [f"- {s['store']} ({s['city']}): {s['message']}" for s in new_hits]
            body = "\n".join(lines)
            log.info(f"[{ts}] {name}: STOCK FOUND\n{body}")
            notify(f"In stock: {name}", body)
            for s in new_hits:
                _already_notified.add((name, s["store"]))
        else:
            log.info(f"[{ts}] {name}: still available at {len(stores)} known store(s), already notified")


if __name__ == "__main__":
    log.info(
        f"Starting watcher for {len(PRODUCTS)} product(s) near {ZIP_CODE}, "
        f"checking every {CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop."
    )
    run_check()  # check once immediately

    scheduler = BlockingScheduler()
    scheduler.add_job(run_check, "interval", seconds=CHECK_INTERVAL_SECONDS)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")
