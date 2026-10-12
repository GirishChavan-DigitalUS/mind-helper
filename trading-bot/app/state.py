"""Ephemeral crash-recovery state.

Holds tracked open positions plus the running daily realized P&L. Written to a
gitignored JSON file so a mid-trade restart can resume exit management. Webull
remains the source of truth; on startup we reconcile against it.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path

from .models import AlertRecord, Position, TradeRecord

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"

MAX_ALERTS = 100
MAX_TRADES = 200


class StateStore:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.positions: dict[str, Position] = {}   # keyed by underlying symbol
        self.daily_pnl: float = 0.0
        self.pnl_day: str = date.today().isoformat()
        self.alerts: list[dict] = []               # recent inbound alerts (newest first)
        self.closed_trades: list[dict] = []        # closed positions (newest first)
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.daily_pnl = float(raw.get("daily_pnl", 0.0))
        self.pnl_day = raw.get("pnl_day", date.today().isoformat())
        self.alerts = list(raw.get("alerts") or [])
        self.closed_trades = list(raw.get("closed_trades") or [])
        for sym, pd in (raw.get("positions") or {}).items():
            try:
                self.positions[sym] = Position.from_dict(pd)
            except (TypeError, ValueError):
                continue

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        payload = {
            "daily_pnl": self.daily_pnl,
            "pnl_day": self.pnl_day,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "alerts": self.alerts,
            "closed_trades": self.closed_trades,
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # os.replace is atomic but can transiently fail with a lock on Windows
        # (e.g. antivirus scanning the temp file); retry a few times.
        for attempt in range(5):
            try:
                tmp.replace(self._path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)



    # ---- mutations ---------------------------------------------------------
    def roll_day_if_needed(self) -> None:
        today = date.today().isoformat()
        with self._lock:
            if today != self.pnl_day:
                self.pnl_day = today
                self.daily_pnl = 0.0
                self._flush()

    def add_position(self, pos: Position) -> None:
        with self._lock:
            self.positions[pos.underlying] = pos
            self._flush()

    def get(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def has(self, symbol: str) -> bool:
        return symbol in self.positions

    def remove(self, symbol: str) -> None:
        with self._lock:
            self.positions.pop(symbol, None)
            self._flush()

    def record_realized(self, symbol: str, pnl: float) -> None:
        with self._lock:
            self.daily_pnl += pnl
            p = self.positions.get(symbol)
            if p:
                p.realized_pnl += pnl
            self._flush()

    def record_alert(self, alert: AlertRecord) -> None:
        with self._lock:
            self.alerts.insert(0, alert.to_dict())
            del self.alerts[MAX_ALERTS:]
            self._flush()

    def record_closed_trade(self, pos: Position) -> None:
        with self._lock:
            trade = TradeRecord(
                underlying=pos.underlying,
                action=pos.action,
                option_symbol=pos.option_symbol,
                strike=pos.strike,
                expiry=pos.expiry,
                basis=pos.basis,
                contracts_total=pos.contracts_total,
                realized_pnl=round(pos.realized_pnl, 2),
                signal=pos.signal,
                opened_at=pos.opened_at,
                closed_at=datetime.now(UTC).isoformat(),
                exits=list(pos.exits),
            )
            self.closed_trades.insert(0, trade.to_dict())
            del self.closed_trades[MAX_TRADES:]
            self._flush()

    def save(self) -> None:
        with self._lock:
            self._flush()

    def open_count(self) -> int:
        return len(self.positions)

