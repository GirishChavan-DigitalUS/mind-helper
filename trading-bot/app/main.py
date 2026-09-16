"""FastAPI app: receives TradingView JSON alerts and drives entries/exits.

Flow:
  TradingView (5m) --alert() JSON--> Cloudflare Tunnel --> POST /webhook
    -> validate shared secret (constant-time)
    -> dedupe (ticker|signal|action|bar_time)
    -> Strategy.handle_signal (universe/risk/spread gates -> enter)
  Background ExitEngine manages scale-outs/trailing/stop on a 1-minute cadence.
On startup, open positions are reconciled from Webull (source of truth).
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from .config import load_params, load_secrets
from .models import AlertRecord, Position, Signal
from .order_events import OrderEventSubscriber
from .position_manager import ExitEngine
from .state import StateStore
from .strategy import Strategy
from .webull_client import WebullClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

SECRETS = load_secrets()
STATE = StateStore()
CLIENT = WebullClient(SECRETS)
STRATEGY = Strategy(CLIENT, STATE)
ENGINE = ExitEngine(CLIENT, STATE, params_provider=load_params)
ORDER_EVENTS = OrderEventSubscriber(SECRETS, STATE)

# dry-run: offline sim | shadow: real-time data + simulated orders | live: real orders
MODE = "dry-run" if SECRETS.dry_run else ("shadow" if SECRETS.shadow else "live")

# simple in-memory dedupe with TTL
_SEEN: dict[str, float] = {}
_DEDUPE_TTL = 300.0


def _dedupe(key: str) -> bool:
    """Return True if this key was seen recently (i.e., a duplicate)."""
    now = time.time()
    for k, ts in list(_SEEN.items()):
        if now - ts > _DEDUPE_TTL:
            _SEEN.pop(k, None)
    if key in _SEEN:
        return True
    _SEEN[key] = now
    return False


def _reconcile_startup() -> None:
    """Drop tracked positions no longer held; log broker-held untracked ones."""
    try:
        held = CLIENT.get_option_positions()
    except Exception:
        log.exception("startup reconciliation: could not fetch broker positions")
        return
    held_syms = {str(h.get("symbol") or h.get("ticker") or "").upper() for h in held}
    for sym in list(STATE.positions.keys()):
        if held_syms and sym not in held_syms:
            log.warning("reconcile: %s tracked locally but not held at broker; dropping", sym)
            STATE.remove(sym)
    for h in held:
        sym = str(h.get("symbol") or h.get("ticker") or "").upper()
        if sym and not STATE.has(sym):
            log.warning("reconcile: broker holds %s but it is untracked (manage manually)", sym)


@asynccontextmanager
async def lifespan(app: FastAPI):
    CLIENT.authenticate()
    if not SECRETS.dry_run and not SECRETS.shadow:
        _reconcile_startup()
    ORDER_EVENTS.start()
    ENGINE.start()
    log.info("started (mode=%s, account=%s)", MODE, CLIENT.account_id)
    try:
        yield
    finally:
        await ENGINE.stop()
        STATE.save()
        log.info("stopped; state saved")


app = FastAPI(title="TV->Webull Options Auto-Trader", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse)
def dashboard() -> Response:
    index = STATIC_DIR / "dashboard.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": MODE,
        "dry_run": SECRETS.dry_run,
        "shadow": SECRETS.shadow,
        "account_id": CLIENT.account_id,
        "open_positions": STATE.open_count(),
        "daily_pnl": round(STATE.daily_pnl, 2),
    }


@app.get("/positions")
def positions() -> dict:
    return {s: p.to_dict() for s, p in STATE.positions.items()}


@app.get("/api/state")
def api_state() -> dict:
    """Everything the dashboard needs in one poll."""
    open_positions = [p.to_dict() for p in STATE.positions.values()]
    unrealized = round(sum(p.unrealized_pnl for p in STATE.positions.values()), 2)
    realized_open = round(sum(p.realized_pnl for p in STATE.positions.values()), 2)
    closed_realized = round(sum(float(t.get("realized_pnl", 0)) for t in STATE.closed_trades), 2)
    wins = sum(1 for t in STATE.closed_trades if float(t.get("realized_pnl", 0)) > 0)
    losses = sum(1 for t in STATE.closed_trades if float(t.get("realized_pnl", 0)) < 0)
    return {
        "health": {
            "status": "ok",
            "mode": MODE,
            "dry_run": SECRETS.dry_run,
            "shadow": SECRETS.shadow,
            "account_id": CLIENT.account_id,
            "open_positions": STATE.open_count(),
            "daily_pnl": round(STATE.daily_pnl, 2),
        },
        "summary": {
            "open_count": STATE.open_count(),
            "unrealized_pnl": unrealized,
            "realized_open_pnl": realized_open,
            "closed_realized_pnl": closed_realized,
            "daily_pnl": round(STATE.daily_pnl, 2),
            "closed_count": len(STATE.closed_trades),
            "wins": wins,
            "losses": losses,
        },
        "positions": open_positions,
        "alerts": STATE.alerts,
        "closed_trades": STATE.closed_trades,
        "exit_params": load_params().get("exit", {}),
    }


def _parse_signal(payload: dict) -> Signal:
    return Signal(
        ticker=str(payload["ticker"]).upper(),
        action=str(payload["action"]).upper(),
        signal=str(payload.get("signal", "")),
        price=float(payload["price"]),
        atr=_maybe_float(payload.get("atr")),
        tf=payload.get("tf"),
        bar_time=_maybe_int(payload.get("time")),
    )


def _maybe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _maybe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    raw = (await request.body()).decode("utf-8", "ignore").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _json({"accepted": False, "reason": "invalid JSON"}, 400)

    # ---- auth: constant-time shared-secret check (body field or header) ----
    provided = str(payload.get("secret", "")) or request.headers.get("X-Webhook-Secret", "")
    if not SECRETS.webhook_secret or not hmac.compare_digest(provided, SECRETS.webhook_secret):
        log.warning("rejected webhook: bad secret from %s", request.client.host if request.client else "?")
        return _json({"accepted": False, "reason": "unauthorized"}, 401)

    required = ("ticker", "action", "price")
    if not all(k in payload for k in required):
        return _json({"accepted": False, "reason": f"missing one of {required}"}, 400)
    if str(payload["action"]).upper() not in ("CALL", "PUT"):
        return _json({"accepted": False, "reason": "action must be CALL or PUT"}, 400)

    sig = _parse_signal(payload)
    if _dedupe(sig.dedupe_key()):
        STATE.record_alert(AlertRecord(sig.ticker, sig.action, sig.signal,
                                       sig.price, False, "duplicate"))
        return _json({"accepted": False, "reason": "duplicate"}, 200)

    params = load_params()  # hot reload per alert
    result = STRATEGY.handle_signal(sig, params)
    STATE.record_alert(AlertRecord(sig.ticker, sig.action, sig.signal,
                                   sig.price, result.accepted, result.reason))
    return _json(result.as_dict(), 200 if result.accepted else 200)


def _json(obj: dict, status: int) -> Response:
    return Response(content=json.dumps(obj), media_type="application/json", status_code=status)
