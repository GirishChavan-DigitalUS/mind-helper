"""Exit engine: premium-percentage scale-outs, breakeven, trailing, stop-loss.

Rules (defaults, all configurable in config.yaml):
  * Initial stop-loss: if premium <= -20% of basis, close all.
  * Breakeven: once premium >= +20%, move the stop to breakeven (entry basis).
  * TP1: at +30%, sell 4 contracts AND arm a 10% trailing stop on the remainder.
  * TP2: at +50%, sell 4 more (the trailing stop keeps running underneath).
  * Trailing: after TP1, everything left trails 10% off the peak premium.

Runs as a background task on a 1-minute cadence. In dry-run, fills are simulated
by the WebullClient; drive `client.set_sim_price(...)` to exercise the logic.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import OptionContract, Position, Quote
from .state import StateStore
from .webull_client import SIDE_SELL_CLOSE, WebullClient

log = logging.getLogger("exits")

CONTRACT_MULTIPLIER = 100  # US equity options


@dataclass
class ExitAction:
    qty: int
    reason: str


class ExitEngine:
    def __init__(self, client: WebullClient, state: StateStore,
                params_provider) -> None:
        self.client = client
        self.state = state
        self._params_provider = params_provider   # callable -> fresh params dict
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="exit-engine")
            log.info("exit engine started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            params = self._params_provider()
            interval = float(params.get("exit", {}).get("monitor_interval_sec", 60))
            try:
                self.run_once(params)
            except Exception:  # never let the loop die
                log.exception("exit cycle error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ cycle
    def run_once(self, params: dict) -> None:
        for symbol in list(self.state.positions.keys()):
            pos = self.state.get(symbol)
            if not pos or pos.contracts_open <= 0:
                continue
            contract = OptionContract(pos.underlying, pos.option_symbol,
                                    pos.action, pos.strike, pos.expiry)
            quote = self.client.get_quote(contract)
            if quote.mid > 0 and abs(pos.last_mark - quote.mid) > 1e-9:
                pos.last_mark = quote.mid
                self.state.save()
            self._evaluate(pos, quote, params)

    def _evaluate(self, pos: Position, quote: Quote, params: dict) -> None:
        cfg = params.get("exit", {})
        cur = quote.mid
        if cur <= 0 or pos.basis <= 0:
            return
        pnl_pct = (cur - pos.basis) / pos.basis * 100.0

        sl = float(cfg.get("stop_loss_pct", 20.0))
        be_trig = float(cfg.get("breakeven_trigger_pct", 20.0))
        tp1 = float(cfg.get("tp1_pct", 30.0))
        tp1_n = int(cfg.get("tp1_contracts", 4))
        tp2 = float(cfg.get("tp2_pct", 50.0))
        tp2_n = int(cfg.get("tp2_contracts", 4))
        trail = float(cfg.get("trailing_pct", cfg.get("runner_trailing_pct", 10.0)))

        # -------- Pre-TP1: stop-loss, escalating to breakeven at +be_trig -----
        if not pos.tp1_done:
            if not pos.be_armed and pnl_pct >= be_trig:
                pos.be_armed = True
                self.state.save()
                log.info("BREAKEVEN armed %s at +%.1f%% (stop -> basis %.2f)",
                         pos.underlying, pnl_pct, pos.basis)
            stop_ref = 0.0 if pos.be_armed else -abs(sl)
            if pnl_pct <= stop_ref:
                self._close(pos, pos.contracts_open, cur,
                            "BREAKEVEN stop" if pos.be_armed else f"STOP {pnl_pct:.1f}%")
                return
            # TP1 scale-out; arms the trailing stop on the remainder
            if pnl_pct >= tp1:
                n = min(tp1_n, pos.contracts_open)
                self._close(pos, n, cur, f"TP1 +{pnl_pct:.1f}%")
                pos.tp1_done = True
                pos.peak = cur
                self.state.save()

        # -------- Post-TP1: TP2 target + 10% trailing stop on remainder -------
        if pos.tp1_done and pos.contracts_open > 0:
            if cur > pos.peak:
                pos.peak = cur
                self.state.save()
            # take the TP2 slice on the way up
            if not pos.tp2_done and pnl_pct >= tp2:
                n = min(tp2_n, pos.contracts_open)
                self._close(pos, n, cur, f"TP2 +{pnl_pct:.1f}%")
                pos.tp2_done = True
                self.state.save()
            # trailing stop (active for everything left after TP1)
            if pos.contracts_open > 0 and pos.peak > 0 and cur <= pos.peak * (1 - trail / 100.0):
                self._close(pos, pos.contracts_open, cur,
                            f"TRAIL -{trail:.0f}% off {pos.peak:.2f}")

    # ------------------------------------------------------------------ helper
    def _close(self, pos: Position, qty: int, price: float, reason: str) -> None:
        qty = min(qty, pos.contracts_open)
        if qty <= 0:
            return
        contract = OptionContract(pos.underlying, pos.option_symbol,
                                pos.action, pos.strike, pos.expiry)
        # marketable sell: price at/just below current for a fill
        sell_limit = round(max(0.01, price * 0.98), 2)
        order = self.client.place_option_order(
            contract, side=SIDE_SELL_CLOSE, qty=qty, limit_price=sell_limit)
        fill = float(order.get("fill_price") or price)
        realized = (fill - pos.basis) * qty * CONTRACT_MULTIPLIER
        pos.contracts_open -= qty
        pos.last_mark = fill
        pos.client_order_ids.append(order["client_order_id"])
        pos.exits.append({
            "qty": qty,
            "price": round(fill, 4),
            "reason": reason,
            "realized": round(realized, 2),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self.state.record_realized(pos.underlying, realized)
        log.info("EXIT %s %s x%d @%.2f | %s | realized $%.2f | open=%d",
                 pos.underlying, pos.action, qty, fill, reason, realized,
                 pos.contracts_open)
        if pos.contracts_open <= 0:
            self.state.record_closed_trade(pos)
            self.state.remove(pos.underlying)
            log.info("CLOSED %s (position flat)", pos.underlying)
