"""Entry strategy: validate a signal, gate on risk/spread, size, and enter."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .models import Position, Signal
from .state import StateStore
from .webull_client import SIDE_BUY_OPEN, WebullClient

log = logging.getLogger("strategy")


class EntryResult:
    def __init__(self, accepted: bool, reason: str, position: Optional[Position] = None) -> None:
        self.accepted = accepted
        self.reason = reason
        self.position = position

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"accepted": self.accepted, "reason": self.reason}
        if self.position:
            d["position"] = self.position.to_dict()
        return d


class Strategy:
    def __init__(self, client: WebullClient, state: StateStore) -> None:
        self.client = client
        self.state = state

    # ------------------------------------------------------------------ gates
    def _is_rth(self, params: dict) -> bool:
        sess = params.get("session", {})
        tz = ZoneInfo(sess.get("timezone", "America/New_York"))
        now = datetime.now(tz)
        if now.weekday() >= 5:  # Sat/Sun
            return False
        start = self._parse_hhmm(sess.get("rth_start", "09:30"))
        end = self._parse_hhmm(sess.get("rth_end", "16:00"))
        return start <= (now.hour, now.minute) <= end

    @staticmethod
    def _parse_hhmm(s: str) -> tuple[int, int]:
        h, m = s.split(":")
        return int(h), int(m)

    def check_gates(self, sig: Signal, params: dict) -> Optional[str]:
        """Return a rejection reason, or None if the signal may proceed."""
        universe = {u.upper() for u in params.get("universe", [])}
        if sig.ticker.upper() not in universe:
            return f"{sig.ticker} not in universe"
        risk = params.get("risk", {})
        if risk.get("rth_only", True) and not self._is_rth(params):
            return "outside regular trading hours"
        self.state.roll_day_if_needed()
        max_loss = float(risk.get("max_daily_loss", 0) or 0)
        if max_loss > 0 and self.state.daily_pnl <= -abs(max_loss):
            return f"daily loss limit hit ({self.state.daily_pnl:.2f})"
        if risk.get("one_position_per_symbol", True) and self.state.has(sig.ticker.upper()):
            return f"already in a position for {sig.ticker}"
        max_conc = int(risk.get("max_concurrent_positions", 999))
        if self.state.open_count() >= max_conc:
            return f"max concurrent positions reached ({max_conc})"
        return None

    # ------------------------------------------------------------------ entry
    def handle_signal(self, sig: Signal, params: dict) -> EntryResult:
        reason = self.check_gates(sig, params)
        if reason:
            log.info("REJECT %s %s: %s", sig.ticker, sig.action, reason)
            return EntryResult(False, reason)

        contract_cfg = params.get("contract", {})
        entry_cfg = params.get("entry", {})
        tz = params.get("session", {}).get("timezone", "America/New_York")

        contract = self.client.select_contract(
            underlying=sig.ticker.upper(),
            action=sig.action,
            price=sig.price,
            expiry_rule=contract_cfg.get("expiry", "nearest_weekly"),
            tz=tz,
        )
        quote = self.client.get_quote(contract)

        max_spread = float(entry_cfg.get("max_spread_pct", 10.0))
        if quote.spread_pct > max_spread:
            r = f"spread {quote.spread_pct:.1f}% > {max_spread:.1f}% max"
            log.info("REJECT %s %s: %s", sig.ticker, sig.action, r)
            return EntryResult(False, r)

        qty = int(entry_cfg.get("contracts", 10))
        slip = float(entry_cfg.get("limit_slippage_pct", 2.0))
        fill_ref = str(entry_cfg.get("fill_price", "mid")).lower()

        # entry pricing reference: market | bid | ask | mid
        if fill_ref == "market":
            order_type, limit = "MARKET", None
        else:
            base = {"bid": quote.bid, "ask": quote.ask, "mid": quote.mid}.get(fill_ref, quote.mid)
            order_type, limit = "LIMIT", round(base * (1 + slip / 100.0), 2)  # buy: buffer up

        order = self.client.place_option_order(
            contract, side=SIDE_BUY_OPEN, qty=qty, limit_price=limit,
            tif=entry_cfg.get("tif", "DAY"), order_type=order_type,
        )
        basis = float(order.get("fill_price") or limit or quote.mid)

        pos = Position(
            underlying=sig.ticker.upper(),
            action=contract.right,
            option_symbol=contract.option_symbol,
            strike=contract.strike,
            expiry=contract.expiry,
            basis=basis,
            contracts_total=qty,
            contracts_open=qty,
            last_mark=basis,
            signal=sig.signal,
            client_order_ids=[order["client_order_id"]],
        )
        self.state.add_position(pos)
        log.info("ENTER %s %s %s x%d @%.2f (%s)", sig.ticker, contract.right,
                 contract.option_symbol, qty, basis, sig.signal)
        return EntryResult(True, "entered", pos)
