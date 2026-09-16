"""Typed data structures shared across the app."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Signal:
    """A parsed, validated inbound entry alert from TradingView."""
    ticker: str
    action: str            # "CALL" | "PUT"
    signal: str            # engine signal name, e.g. "ORB_BREAKOUT_UP"
    price: float           # underlying price at signal
    atr: Optional[float] = None
    tf: Optional[str] = None
    bar_time: Optional[int] = None   # ms epoch of the signal bar
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def dedupe_key(self) -> str:
        return f"{self.ticker}|{self.signal}|{self.action}|{self.bar_time}"


@dataclass
class OptionContract:
    """A resolved option to trade."""
    underlying: str
    option_symbol: str     # broker contract identifier
    right: str             # "CALL" | "PUT"
    strike: float
    expiry: str            # YYYY-MM-DD


@dataclass
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return 0.0
        if self.bid <= 0:
            return self.ask
        if self.ask <= 0:
            return self.bid
        return round((self.bid + self.ask) / 2.0, 4)

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0:
            return 100.0
        return round((self.ask - self.bid) / m * 100.0, 3)


@dataclass
class Position:
    """A live (or simulated) options position tracked by the exit engine."""
    underlying: str
    action: str            # CALL | PUT
    option_symbol: str
    strike: float
    expiry: str
    basis: float           # avg entry premium per share
    contracts_total: int
    contracts_open: int
    tp1_done: bool = False
    tp2_done: bool = False
    be_armed: bool = False  # stop moved to breakeven once +breakeven_trigger_pct hit
    peak: float = 0.0       # peak premium seen (drives the post-TP1 trailing stop)
    realized_pnl: float = 0.0  # USD realized on this position so far
    last_mark: float = 0.0     # last observed option mid (drives unrealized P&L)
    signal: str = ""           # entry signal name
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    client_order_ids: list[str] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)  # per-leg exit records

    @property
    def unrealized_pnl(self) -> float:
        """USD unrealized on the still-open contracts at the last mark."""
        if self.last_mark <= 0 or self.contracts_open <= 0:
            return 0.0
        return round((self.last_mark - self.basis) * self.contracts_open * 100.0, 2)

    @property
    def unrealized_pct(self) -> float:
        if self.basis <= 0 or self.last_mark <= 0:
            return 0.0
        return round((self.last_mark - self.basis) / self.basis * 100.0, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unrealized_pnl"] = self.unrealized_pnl
        d["unrealized_pct"] = self.unrealized_pct
        return d

    @staticmethod
    def from_dict(d: dict) -> "Position":
        d = {k: v for k, v in d.items() if k not in ("unrealized_pnl", "unrealized_pct")}
        return Position(**d)


@dataclass
class AlertRecord:
    """A received TradingView alert and how the strategy handled it."""
    ticker: str
    action: str
    signal: str
    price: float
    accepted: bool
    reason: str
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradeRecord:
    """A fully closed position with realized P&L, for history/reporting."""
    underlying: str
    action: str
    option_symbol: str
    strike: float
    expiry: str
    basis: float
    contracts_total: int
    realized_pnl: float
    signal: str
    opened_at: str
    closed_at: str
    exits: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

