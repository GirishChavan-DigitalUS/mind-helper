"""Live order-fill subscriber (Webull gRPC events).

Subscribes to ORDER_STATUS_CHANGED and, when an ENTRY order fills, updates that
position's ``basis`` to the broker's real average fill price so the exit engine
trails from the true cost. No-op in dry-run (fills are simulated synchronously).

Runs the blocking ``do_subscribe`` on a daemon thread. Field names in the event
payload vary slightly by region and are read defensively.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .config import Secrets
from .state import StateStore

log = logging.getLogger("order-events")


class OrderEventSubscriber:
    def __init__(self, secrets: Secrets, state: StateStore) -> None:
        self._s = secrets
        self._state = state
        self._thread: Optional[threading.Thread] = None
        self._client = None

    def start(self) -> None:
        if self._s.dry_run or getattr(self._s, "shadow", False):
            tag = "SHADOW" if getattr(self._s, "shadow", False) else "DRY_RUN"
            log.info("%s: order-event stream disabled (orders simulated)", tag)
            return
        self._thread = threading.Thread(target=self._run, name="order-events", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from webull.trade.trade_events_client import TradeEventsClient  # type: ignore
            from webull.trade.events.types import (  # type: ignore
                ORDER_STATUS_CHANGED, EVENT_TYPE_ORDER,
            )
        except Exception:
            log.exception("order-event SDK unavailable; live fills will use estimates")
            return

        # TradeEventsClient resolves its endpoint via the ``host`` kwarg
        # (sandbox events endpoint for paper trading).
        client = TradeEventsClient(
            self._s.app_key, self._s.app_secret, self._s.region,
            host=self._s.events_endpoint or None,
        )

        def on_event(event_type, subscribe_type, payload, raw_message):
            try:
                if event_type == EVENT_TYPE_ORDER and subscribe_type == ORDER_STATUS_CHANGED:
                    self._on_order(payload)
            except Exception:
                log.exception("error handling order event")

        client.on_events_message = on_event
        self._client = client
        log.info("subscribing to order events for account %s", self._s.account_id)
        client.do_subscribe([self._s.account_id])

    # ------------------------------------------------------------------ handler
    def _on_order(self, payload: dict) -> None:
        status = str(_get(payload, "status", "order_status", "orderStatus")).upper()
        if status not in ("FILLED", "PARTIAL_FILLED", "PARTIALLY_FILLED"):
            return
        coid = str(_get(payload, "client_order_id", "clientOrderId") or "")
        avg = _to_float(_get(payload, "avg_fill_price", "avgFillPrice",
                            "filled_price", "avg_price"))
        if not coid or avg is None or avg <= 0:
            return
        # match the ENTRY order (basis-setting) for a tracked position
        for sym, pos in list(self._state.positions.items()):
            is_entry = pos.contracts_total == pos.contracts_open
            if coid in pos.client_order_ids and is_entry:
                if abs(pos.basis - avg) > 1e-6:
                    log.info("fill: %s basis %.2f -> %.2f (%s)", sym, pos.basis, avg, status)
                    pos.basis = avg
                    self._state.save()
                return


def _get(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
