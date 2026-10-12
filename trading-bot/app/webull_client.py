"""Webull OpenAPI adapter for options (paper/sandbox), plus a dry-run simulator.

Real calls use the OFFICIAL Webull Python SDK (pip: webull-openapi-python-sdk):
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    from webull.data.data_client import DataClient
    api_client = ApiClient(app_key, app_secret, region_id)
    api_client.add_endpoint(region_id, api_endpoint)
    trade = TradeClient(api_client)
    data = DataClient(api_client)
    trade.account_v2.get_account_list()
    trade.account_v2.get_account_position(account_id)
    trade.order_v3.place_order(account_id, [order_dict])   # option order via legs[]
    data.instrument.get_option_contracts(...)              # option chain
    data.option_market_data.get_option_snapshot(symbols, category)  # option quote

Option market data requires an OpenAPI OPRA subscription; sandbox returns
15-minute delayed data by default. When ``dry_run`` is True (default) no network
calls are made: the class simulates option selection, quotes and fills so the
whole pipeline can be wire-tested. The live option-leg payload is isolated in
``_build_option_order`` so it is easy to reconcile with the Options Orders docs.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Secrets
from .models import OptionContract, Quote

log = logging.getLogger("webull")

SIDE_BUY_OPEN = "BUY"
SIDE_SELL_CLOSE = "SELL"


def past_local_time(hhmm: str, tz: str, now: datetime | None = None) -> bool:
    """True if the wall-clock time in ``tz`` is at/after ``hhmm`` (HH:MM)."""
    z = ZoneInfo(tz)
    now = now.astimezone(z) if now else datetime.now(z)
    h, m = (int(x) for x in hhmm.split(":"))
    return (now.hour, now.minute) >= (h, m)


def next_expiry(rule: str, tz: str = "America/New_York", now: datetime | None = None,
                no_0dte_after: str | None = None, cutoff_tz: str | None = None) -> str:
    """Resolve an expiry date (YYYY-MM-DD) from a rule.

    nearest_weekly  -> upcoming Friday (or today if today is Fri)
    zero_dte        -> today (next weekday if weekend)
    nearest_monthly -> third Friday of the current or next month
    After ``no_0dte_after`` (HH:MM in ``cutoff_tz``) today's expiry is excluded.
    """
    z = ZoneInfo(tz)
    now = now.astimezone(z) if now else datetime.now(z)
    d = now.date()
    if no_0dte_after and past_local_time(no_0dte_after, cutoff_tz or tz, now):
        d += timedelta(days=1)
    if rule == "zero_dte":
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()
    if rule == "nearest_monthly":
        return _third_friday(d).isoformat()
    # nearest_weekly (default)
    ahead = (4 - d.weekday()) % 7          # Friday == 4
    friday = d + timedelta(days=ahead)
    return friday.isoformat()


def _third_friday(now: date):
    year, month = now.year, now.month
    first_wd = datetime(year, month, 1).weekday()
    first_friday = 1 + ((4 - first_wd) % 7)
    third = first_friday + 14
    # if already past this month's third Friday, roll to next month
    if now.day > third:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        first_wd = datetime(year, month, 1).weekday()
        first_friday = 1 + ((4 - first_wd) % 7)
        third = first_friday + 14
    return datetime(year, month, third).date()


def _strike_increment(price: float) -> float:
    if price >= 200:
        return 5.0
    if price >= 100:
        return 1.0
    if price >= 25:
        return 0.5
    return 0.5


class WebullClient:
    def __init__(self, secrets: Secrets) -> None:
        self._s = secrets
        self.dry_run = secrets.dry_run
        self.shadow = getattr(secrets, "shadow", False)
        # Orders are simulated locally in dry-run (fully offline) and shadow
        # (real-time data, no broker orders) modes; only true-live submits.
        self._simulate_orders = self.dry_run or self.shadow
        self.account_id = secrets.account_id or ""
        self._trade: Any = None                   # webull.trade.trade_client.TradeClient
        self._data: Any = None                    # webull.data.data_client.DataClient
        self._sim_prices: dict[str, float] = {}   # option_symbol -> simulated mid

    # ------------------------------------------------------------------ auth
    def authenticate(self) -> None:
        if self.dry_run:
            log.info("DRY_RUN: skipping Webull authentication")
            if not self.account_id:
                self.account_id = "PAPER-SIM"
            return
        from webull.core.client import ApiClient  # type: ignore
        from webull.data.data_client import DataClient  # type: ignore
        from webull.trade.trade_client import TradeClient  # type: ignore

        api_client = ApiClient(self._s.app_key, self._s.app_secret, self._s.region)
        if self._s.api_endpoint:
            api_client.add_endpoint(self._s.region, self._s.api_endpoint)
        self._trade = TradeClient(api_client)
        self._data = DataClient(api_client)
        if not self.account_id:
            if self.shadow:
                # Shadow places no broker orders, so an account id is optional;
                # we only need the DataClient for real-time quotes/chains.
                log.info("SHADOW: no account_id set; reading real-time market "
                         "data only, orders are simulated locally")
            else:
                # In live mode, refuse to guess: production get_account_list()
                # can include real-money accounts, so the target account must be
                # set explicitly (WEBULL_ACCOUNT_ID) to avoid the wrong one.
                available = self._list_accounts()
                raise RuntimeError(
                    "WEBULL_ACCOUNT_ID is not set. Refusing to auto-select an account "
                    "in live mode. Set it in .env to your paper account id. Available "
                    f"accounts: {available}")
        log.info("Webull %s; account_id=%s endpoint=%s",
                 "shadow (live data, simulated orders)" if self.shadow else "authenticated",
                 self.account_id or "-", self._s.api_endpoint)

    def _list_accounts(self) -> list[dict]:
        """Return [{account_id, account_label, account_class}] for selection help."""
        res = self._trade.account_v2.get_account_list()
        data = res.json() if hasattr(res, "json") else res
        rows = data.get("data", data) if isinstance(data, dict) else data
        out: list[dict] = []
        for r in rows or []:
            acct = (r.get("account_id") or r.get("accountId")
                    or r.get("secAccountId") or r.get("id"))
            out.append({
                "account_id": str(acct) if acct else None,
                "account_label": r.get("account_label"),
                "account_class": r.get("account_class"),
            })
        return out

    # -------------------------------------------------------------- contracts
    def select_contract(self, underlying: str, action: str, price: float,
                        expiry_rule: str, tz: str, no_0dte_after: str | None = None,
                        cutoff_tz: str | None = None) -> OptionContract:
        """Pick the 1 ATM contract for the given side and nearest expiry."""
        expiry = next_expiry(expiry_rule, tz, no_0dte_after=no_0dte_after, cutoff_tz=cutoff_tz)
        right = "CALL" if action.upper() == "CALL" else "PUT"
        if self.dry_run:
            inc = _strike_increment(price)
            strike = round(price / inc) * inc
            osym = self._occ_symbol(underlying, expiry, right, strike)
            # seed a plausible ATM premium if not already simulated
            self._sim_prices.setdefault(osym, round(max(0.30, price * 0.008), 2))
            return OptionContract(underlying, osym, right, strike, expiry)
        return self._live_select_contract(underlying, right, price, expiry)

    def _live_select_contract(self, underlying: str, right: str, price: float,
                            expiry: str) -> OptionContract:
        """Query the option chain and choose the strike nearest to spot for ``expiry``.

        Uses DataClient.instrument.get_option_contracts (US_OPTION), narrowed to a
        window around spot to keep the response small.
        """
        if self._data is None:
            raise RuntimeError("Data client not initialized; cannot fetch option chain")
        lo = round(price * 0.90, 2)
        hi = round(price * 1.10, 2)
        res = self._data.instrument.get_option_contracts(
            category="US_OPTION",
            underlying_symbols=underlying,
            start_date=expiry,          # exact expiration date
            option_type=right,          # CALL / PUT
            strike_price_gte=lo,
            strike_price_lte=hi,
            page_size=1000,
        )
        data = res.json() if hasattr(res, "json") else res
        contracts = _extract_contracts(data, right)
        if not contracts:
            raise RuntimeError(f"No {right} contracts for {underlying} {expiry}")
        # The chain ignores start_date (returns many expiries) and includes
        # non-standard FLEX/adjusted contracts with odd strikes and symbols
        # (e.g. 4SPY.../2SPY...) that the snapshot API rejects. Restrict to
        # STANDARD contracts at the exact target expiry before picking a strike.
        pool = [c for c in contracts
                if c.get("expiration_date") == expiry
                and str(c.get("def_type", "STANDARD")).upper() == "STANDARD"]
        if not pool:
            pool = [c for c in contracts if c.get("expiration_date") == expiry] or contracts
        best = min(pool, key=lambda c: abs(float(c["strike"]) - price))
        osym = str(best.get("symbol") or best.get("instrument_id") or best.get("option_symbol"))
        return OptionContract(
            underlying=underlying,
            option_symbol=osym,
            right=right,
            strike=float(best["strike"]),
            expiry=expiry,
        )

    @staticmethod
    def _occ_symbol(underlying: str, expiry: str, right: str, strike: float) -> str:
        y, m, d = expiry.split("-")
        cp = "C" if right == "CALL" else "P"
        return f"{underlying}{y[2:]}{m}{d}{cp}{round(strike * 1000):08d}"

    # ------------------------------------------------------------------ quotes
    def get_quote(self, contract: OptionContract) -> Quote:
        if self.dry_run:
            mid = self._sim_prices.get(contract.option_symbol, 1.0)
            half = max(0.01, mid * 0.015)   # ~3% spread
            return Quote(bid=round(mid - half, 2), ask=round(mid + half, 2))
        if self._data is None:
            raise RuntimeError("Data client not initialized; cannot fetch option quote")
        res = self._data.option_market_data.get_option_snapshot(
            contract.option_symbol, category="US_OPTION")
        data = res.json() if hasattr(res, "json") else res
        bid, ask = _extract_bid_ask(data)
        return Quote(bid=bid, ask=ask)

    # simulation hook used by tests / manual driving of the exit engine
    def set_sim_price(self, option_symbol: str, mid: float) -> None:
        self._sim_prices[option_symbol] = round(mid, 2)

    def _simulated_fill(self, contract: OptionContract, limit_price: float | None) -> float:
        """Estimate a fill for simulated (dry-run / shadow) orders.

        Dry-run uses the seeded synthetic mid; shadow prices off the real-time
        quote (the limit when provided, else the live mid) so simulated PnL
        tracks the actual market.
        """
        if self.dry_run:
            return self._sim_prices.get(contract.option_symbol, limit_price or 1.0)
        if limit_price is not None:
            return limit_price
        try:
            q = self.get_quote(contract)
            return round((q.bid + q.ask) / 2, 2)
        except Exception:  # noqa: BLE001 - any quote failure falls back to an estimate
            return limit_price or 1.0

    # ------------------------------------------------------------------ orders
    def place_option_order(self, contract: OptionContract, side: str, qty: int,
                        limit_price: float | None, tif: str = "DAY",
                        order_type: str = "LIMIT") -> dict:
        """Place an option order. Returns {client_order_id, status, fill_price}.

        ``order_type`` is LIMIT or MARKET. For MARKET, ``limit_price`` is ignored.
        For live orders the true average fill arrives via the order-events stream
        (see order_events.py); the returned fill_price here is only an estimate.
        """
        client_order_id = uuid.uuid4().hex
        if self._simulate_orders:
            fill = self._simulated_fill(contract, limit_price)
            tag = "DRY_RUN" if self.dry_run else "SHADOW"
            log.info("%s order %s %s %s x%d @%s (fill~%.2f) %s",
                     tag, order_type, side, contract.option_symbol, qty,
                     f"{limit_price:.2f}" if limit_price else "MKT", fill, client_order_id)
            return {"client_order_id": client_order_id, "status": "FILLED",
                    "fill_price": fill, "qty": qty}
        order = self._build_option_order(contract, side, qty, limit_price, tif,
                                        client_order_id, order_type)
        res = self._trade.order_v3.place_order(self.account_id, [order])
        data = res.json() if hasattr(res, "json") else res
        status_code = getattr(res, "status_code", 200)
        if status_code != 200:
            raise RuntimeError(f"Order rejected ({status_code}): {data}")
        return {"client_order_id": client_order_id, "status": "SUBMITTED",
                "fill_price": None, "qty": qty, "raw": data}

    def _build_option_order(self, contract: OptionContract, side: str, qty: int,
                            limit_price: float | None, tif: str,
                            client_order_id: str, order_type: str = "LIMIT") -> dict:
        """Build a single-leg option order for order_v3.place_order (official SDK).

        Schema follows the Options Orders docs: instrument_type=OPTION with a
        ``legs`` array. Options support LIMIT / STOP_LOSS / STOP_LOSS_LIMIT only
        (no MARKET, no TRAILING_STOP_LOSS) and BUY / SELL sides only.
        """
        # Options have no MARKET type; coerce to LIMIT when a price is available.
        otype = order_type if order_type in ("LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT") else "LIMIT"
        order: dict = {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": contract.underlying,
            "order_type": otype,
            "quantity": str(qty),
            "side": side,
            "time_in_force": tif,
            "entrust_type": "QTY",
            "legs": [{
                "side": side,
                "quantity": str(qty),
                "symbol": contract.underlying,
                "strike_price": f"{contract.strike:.2f}",
                "option_expire_date": contract.expiry,
                "instrument_type": "OPTION",
                "option_type": contract.right,
                "market": "US",
            }],
        }
        if otype in ("LIMIT", "STOP_LOSS_LIMIT") and limit_price is not None:
            order["limit_price"] = f"{limit_price:.2f}"
        return order

    def get_order_status(self, client_order_id: str) -> dict:
        if self._simulate_orders:
            return {"client_order_id": client_order_id, "status": "FILLED"}
        res = self._trade.order_v3.get_order_detail(self.account_id, client_order_id)
        data = res.json() if hasattr(res, "json") else res
        return data

    def cancel_order(self, client_order_id: str) -> dict:
        if self._simulate_orders:
            return {"client_order_id": client_order_id, "status": "CANCELLED"}
        res = self._trade.order_v3.cancel_order(self.account_id, client_order_id)
        data = res.json() if hasattr(res, "json") else res
        return data

    # ----------------------------------------------------------- reconciliation
    def get_option_positions(self) -> list[dict]:
        """Return currently held option positions (for startup reconciliation)."""
        if self._simulate_orders:
            return []
        res = self._trade.account_v2.get_account_position(self.account_id)
        data = res.json() if hasattr(res, "json") else res
        rows = data.get("data", data) if isinstance(data, dict) else data
        out: list[dict] = []
        for r in rows or []:
            if str(r.get("instrument_type", r.get("assetType", ""))).upper() == "OPTION":
                out.append(r)
        return out


# --- response extraction helpers (tolerant of field-name variance) ----------
def _extract_contracts(data, right: str) -> list[dict]:
    rows = data.get("data", data) if isinstance(data, dict) else data
    out: list[dict] = []
    for r in rows or []:
        rt = str(r.get("option_type") or r.get("direction") or r.get("call_put") or "").upper()
        if rt.startswith(right[0]):
            if "strike" not in r:
                r = {**r, "strike": r.get("strike_price") or r.get("strikePrice")}
            out.append(r)
    return out


def _extract_bid_ask(data) -> tuple[float, float]:
    row = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(row, list) and row:
        row = row[0]
    bid = float(row.get("bid") or row.get("bid_price") or 0.0)
    ask = float(row.get("ask") or row.get("ask_price") or 0.0)
    return bid, ask
