"""Phase 0 smoke test: verify Webull OpenAPI paper access end to end.

Steps:
  1. Authenticate with app_key/app_secret against the configured (sandbox) endpoint.
  2. Resolve the paper account_id.
  3. Resolve the nearest-weekly ATM contract for a sample symbol.
  4. Fetch a quote (spread sanity check).
  5. Place a 1-lot BUY_OPEN, then cancel it.

Run:
    # dry-run (no network) just checks wiring:
    python -m scripts.smoke_test SPY
    # live paper: set DRY_RUN=false and real creds in .env first
"""
from __future__ import annotations

import sys

from app.config import load_params, load_secrets
from app.webull_client import SIDE_BUY_OPEN, WebullClient, next_expiry


def main(symbol: str = "SPY", price: float = 500.0) -> None:
    secrets = load_secrets()
    params = load_params()
    client = WebullClient(secrets)

    print(f"[1] authenticate (dry_run={secrets.dry_run}) ...")
    client.authenticate()
    print(f"    account_id = {client.account_id}")

    tz = params.get("session", {}).get("timezone", "America/New_York")
    rule = params.get("contract", {}).get("expiry", "nearest_weekly")
    print(f"[2] nearest expiry ({rule}) = {next_expiry(rule, tz)}")

    print(f"[3] select ATM CALL for {symbol} @ {price} ...")
    contract = client.select_contract(symbol, "CALL", price, rule, tz)
    print(f"    {contract}")

    print("[4] quote ...")
    q = client.get_quote(contract)
    print(f"    bid={q.bid} ask={q.ask} mid={q.mid} spread={q.spread_pct}%")

    print("[5] place 1-lot BUY_OPEN then cancel ...")
    order = client.place_option_order(contract, SIDE_BUY_OPEN, qty=1,
                                    limit_price=round(q.ask * 1.02, 2))
    print(f"    order = {order}")
    if not secrets.dry_run:
        status = client.get_order_status(order["client_order_id"])
        print(f"    status = {status}")
        print("    NOTE: cancel via trade.order_v3.cancel_order(account_id, client_order_id)")
    print("SMOKE TEST COMPLETE")


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    px = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
    main(sym, px)
