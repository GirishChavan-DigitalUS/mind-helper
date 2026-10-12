#!/usr/bin/env python3
"""
iPhone Store Availability Watcher - Web Dashboard
--------------------------------------------------
Reuses CONFIG, check_product() and notify() from iphone_watcher.py.
A background job polls Apple every 60s; the page auto-refreshes every 60s.

Run:
    pip install flask requests apscheduler
    python iphone_watcher_web.py
Open:
    http://127.0.0.1:5000
"""

import threading
import time
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template_string

import iphone_watcher as w

REFRESH_SECONDS = 60
SUMMARY_MINUTES = 15
RETRY_DELAY_SECONDS = 5

app = Flask(__name__)
_lock = threading.Lock()
# Prevents a manual refresh and a scheduled one from running at once (duplicate alerts).
_refresh_lock = threading.Lock()
# Product name -> store names that had pickup stock on the last successful check.
_last_stores: dict[str, set[str]] = {}
_status: dict[str, Any] = {
    "zip": w.ZIP_CODE,
    "interval": REFRESH_SECONDS,
    "last_checked": None,
    "products": [],
}


def refresh() -> None:
    with _refresh_lock:
        _refresh()


def _check_with_retry(name: str, part: str) -> list[dict]:
    # Apple intermittently answers 541 under repeated polling; one delayed retry usually succeeds.
    try:
        return w.check_product(name, part)
    except Exception as e:  # noqa: BLE001 - retry once on any failure
        w.log.warning(f"{name}: check failed ({e}), retrying in {RETRY_DELAY_SECONDS}s")
        time.sleep(RETRY_DELAY_SECONDS)
        return w.check_product(name, part)


def _refresh() -> None:
    with _lock:
        previous = {p["name"]: p for p in _status["products"]}

    results = []
    for name, part in w.PRODUCTS.items():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry: dict[str, Any] = {
            "name": name,
            "part": part,
            "buy_url": f"https://www.apple.com/{w.COUNTRY}/shop/product/{part}",
            "stores": [],
            "updated": None,
            "error": None,
        }
        try:
            entry["stores"] = _check_with_retry(name, part)
            entry["updated"] = now
        except Exception as e:  # noqa: BLE001 - keep last known stock on any failure
            entry["error"] = str(e)
            w.log.error(f"{name}: check failed after retry ({e})")
            prev = previous.get(name)
            if prev and prev["updated"]:
                entry["stores"] = prev["stores"]
                entry["updated"] = prev["updated"]
        else:
            _alert_changes(name, entry["buy_url"], entry["stores"])
        results.append(entry)

    with _lock:
        _status["products"] = results
        _status["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _alert_changes(name: str, buy_url: str, stores: list[dict]) -> None:
    current = {s["store"]: s for s in stores}
    previous = _last_stores.get(name, set())
    _last_stores[name] = set(current)

    added = [s for k, s in current.items() if k not in previous]
    removed = sorted(previous - current.keys())
    kept = [k for k in current if k in previous]
    if not added and not removed:
        w.log.info(f"{name}: no change ({len(current)} store(s) with pickup stock)")
        return

    lines = []
    if added:
        lines.append("New stock:")
        lines += [f"- {s['store']} ({s['city']}): {s['message']}" for s in added]
    if removed:
        lines.append("Sold out:")
        lines += [f"- {k}" for k in removed]
    if kept:
        lines.append("Still available:")
        lines += [f"- {k}" for k in kept]

    if added:
        title, priority = f"In stock: {name}", "urgent"
    elif current:
        title, priority = f"Stock update: {name}", "default"
    else:
        title, priority = f"Sold out: {name}", "default"

    body = "\n".join(lines)
    w.log.info(f"{title}\n{body}")
    links = [("Buy on apple.com", buy_url)] if current else None
    w.notify(title, body, priority=priority, links=links)


def send_summary() -> None:
    with _lock:
        products = list(_status["products"])
        checked = _status["last_checked"]
    if not checked:
        return

    lines = []
    links = []
    for p in products:
        stale = " (stale)" if p["error"] else ""
        if p["error"] and not p["updated"]:
            lines.append(f"{p['name']}: check failed")
        elif p["stores"]:
            names = ", ".join(s["store"] for s in p["stores"])
            lines.append(f"{p['name']}: {len(p['stores'])} store(s) - {names}{stale}")
            links.append((f"Buy {p['name']}", p["buy_url"]))
        else:
            lines.append(f"{p['name']}: not available{stale}")
    lines.append(f"Last checked {checked}")
    w.notify("iPhone stock summary", "\n".join(lines), priority="low", links=links or None)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iPhone Availability Watcher</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f5f5f7; color:#1d1d1f; margin:0; padding:24px; }
  h1 { margin:0 0 4px; font-size:24px; }
  .meta { color:#6e6e73; margin-bottom:20px; font-size:14px; }
  .card { background:#fff; border-radius:14px; padding:18px 20px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
  .head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .name { font-weight:600; font-size:17px; }
  .part { color:#6e6e73; font-size:13px; }
  .badge { padding:4px 10px; border-radius:999px; font-size:13px; font-weight:600; white-space:nowrap; }
  .ok { background:#d1f5d8; color:#0a7d24; }
  .no { background:#eee; color:#6e6e73; }
  .err { background:#fde2e1; color:#c0271b; }
  ul { margin:12px 0 0; padding-left:20px; }
  li { margin:4px 0; }
  .errmsg { color:#c0271b; font-size:13px; margin-top:8px; }
  .right { display:flex; align-items:center; gap:10px; }
  .buy { background:#0071e3; color:#fff; text-decoration:none; padding:5px 12px; border-radius:999px; font-size:13px; font-weight:600; white-space:nowrap; }
  .buy:hover { background:#0077ed; }
  #refreshBtn { margin-left:10px; background:#0071e3; color:#fff; border:0; padding:5px 14px; border-radius:999px; font-size:13px; font-weight:600; cursor:pointer; }
  #refreshBtn:disabled { background:#999; cursor:default; }
</style>
</head>
<body>
<h1>iPhone Availability Watcher</h1>
<div class="meta">
  Near <b id="zip">-</b> &middot; Last checked: <b id="last">-</b> &middot; Next refresh in <b id="countdown">-</b>s
  <button id="refreshBtn" type="button">Refresh now</button>
</div>
<div id="products"></div>

<script>
const INTERVAL = {{ interval }};
let remaining = INTERVAL;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function render(d) {
  document.getElementById("zip").textContent = d.zip;
  document.getElementById("last").textContent = d.last_checked || "pending...";
  const root = document.getElementById("products");
  root.replaceChildren();
  for (const p of d.products) {
    const card = el("div", "card");
    const head = el("div", "head");
    const left = el("div");
    left.append(el("div", "name", p.name), el("div", "part", p.part));
    let badge;
    if (p.error && !p.updated) badge = el("span", "badge err", "Error");
    else if (p.stores.length) badge = el("span", "badge ok", `In stock at ${p.stores.length} store(s)`);
    else badge = el("span", "badge no", "Not available");
    const right = el("div", "right");
    right.append(badge);
    if (p.stores.length) {
      const buy = el("a", "buy", "Buy on apple.com");
      buy.href = p.buy_url;
      buy.target = "_blank";
      buy.rel = "noopener noreferrer";
      right.append(buy);
    }
    head.append(left, right);
    card.append(head);
    if (p.error) {
      const msg = p.updated
        ? `Latest check failed (Apple is throttling). Showing result from ${p.updated}.`
        : p.error;
      card.append(el("div", "errmsg", msg));
    }
    if (p.stores.length) {
      const ul = el("ul");
      for (const s of p.stores) ul.append(el("li", null, `${s.store} (${s.city}) - ${s.message}`));
      card.append(ul);
    }
    root.append(card);
  }
}

async function load(url = "/api/status", method = "GET") {
  try {
    const r = await fetch(url, { method, cache: "no-store" });
    render(await r.json());
  } catch (e) {
    document.getElementById("last").textContent = "failed to load status";
  }
  remaining = INTERVAL;
}

const btn = document.getElementById("refreshBtn");
btn.addEventListener("click", async () => {
  btn.disabled = true;
  btn.textContent = "Checking...";
  await load("/api/refresh", "POST");
  btn.disabled = false;
  btn.textContent = "Refresh now";
});

setInterval(() => {
  remaining -= 1;
  if (remaining <= 0) load();
  document.getElementById("countdown").textContent = Math.max(remaining, 0);
}, 1000);

load();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PAGE, interval=REFRESH_SECONDS)


@app.route("/api/status")
def status():
    with _lock:
        return jsonify(_status)


@app.route("/api/refresh", methods=["POST"])
def manual_refresh():
    refresh()
    return status()


if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh, "interval", seconds=REFRESH_SECONDS, next_run_time=datetime.now())
    scheduler.add_job(send_summary, "interval", minutes=SUMMARY_MINUTES)
    scheduler.start()
    try:
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown()
