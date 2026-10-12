r"""Read-only live Webull auth check (NO orders placed).

Confirms, against the real (sandbox/paper) Webull OpenAPI, exactly what your
app key + account can do BEFORE relying on it during market hours:

  1. Credentials are present in .env
  2. ApiClient authenticates against the configured endpoint (signed request)
  3. account_v2.get_account_list()      -> can we see an account?
  4. account_v2.get_account_position()  -> can we read positions?

This script places NO orders and modifies nothing. It is safe to run anytime.

Run (PowerShell):
    # set live paper creds + DRY_RUN=false in .env first, then:
    .\.venv\Scripts\python.exe -m scripts.live_auth_check

Interpreting results:
  * "not available to Webull US" / permission errors  -> US options OpenAPI not
    enabled for this account/app; live trading is blocked by Webull (not code).
  * A ServerException with an auth/signature code       -> key/secret/region or
    endpoint mismatch.
  * Success with an account_id printed                  -> auth + account access
    works; next step is confirming option data/order entitlements.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from app.config import load_secrets


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _as_json(res) -> object:
    """Best-effort extract of a JSON body from an SDK response."""
    if hasattr(res, "json"):
        try:
            return res.json()
        except Exception:  # noqa: BLE001, S110 - fall through to text
            pass
    return getattr(res, "text", res)


def main() -> int:
    secrets = load_secrets()

    print("=" * 64)
    print("Webull LIVE auth check (read-only, no orders)")
    print("=" * 64)
    print(f"  region        = {secrets.region}")
    print(f"  api_endpoint  = {secrets.api_endpoint}")
    print(f"  app_key       = {_mask(secrets.app_key)}")
    print(f"  app_secret    = {_mask(secrets.app_secret)}")
    print(f"  account_id    = {secrets.account_id or '<will resolve>'}")
    print(f"  DRY_RUN       = {secrets.dry_run}")
    print("-" * 64)

    if secrets.dry_run:
        print("DRY_RUN is TRUE -> no live call will be made. Set DRY_RUN=false in")
        print(".env to actually test live auth. Aborting (nothing to verify live).")
        return 2

    if not secrets.app_key or not secrets.app_secret:
        print("ERROR: WEBULL_APP_KEY / WEBULL_APP_SECRET missing in .env. Aborting.")
        return 2

    # --- 1) authenticate -----------------------------------------------------
    try:
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        from webull.trade.trade_client import TradeClient
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR importing Webull SDK: {exc!r}")
        print("Is the venv active / is webull-openapi-python-sdk installed?")
        return 3

    print("[1] building ApiClient + TradeClient + DataClient ...")
    api_client = ApiClient(secrets.app_key, secrets.app_secret, secrets.region)
    if secrets.api_endpoint:
        api_client.add_endpoint(secrets.region, secrets.api_endpoint)
    trade = TradeClient(api_client)
    data = DataClient(api_client)
    print("    ok (client constructed; auth is verified on first request)")

    # --- 2) get_account_list -------------------------------------------------
    print("[2] account_v2.get_account_list() ...")
    account_id = secrets.account_id or ""
    try:
        res = trade.account_v2.get_account_list()
        data_body = _as_json(res)
        print("    RESPONSE:")
        print(_indent(json.dumps(data_body, indent=2, default=str)))
        rows = data_body.get("data", data_body) if isinstance(data_body, dict) else data_body
        if isinstance(rows, list) and rows:
            first = rows[0]
            account_id = str(
                first.get("account_id") or first.get("accountId")
                or first.get("secAccountId") or first.get("id") or account_id
            )
            print(f"    -> resolved account_id = {account_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print("    (An auth/signature error means key/secret/region/endpoint are")
        print("     wrong. A 'not available to Webull US' message means the trade")
        print("     API is not enabled for US accounts yet.)")
        return 4

    # --- 3) get_account_position (read-only) ---------------------------------
    if account_id:
        print(f"[3] account_v2.get_account_position({account_id}) ...")
        try:
            res = trade.account_v2.get_account_position(account_id)
            print("    RESPONSE:")
            print(_indent(json.dumps(_as_json(res), indent=2, default=str)))
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            print("    (Positions read failed - note this for reconciliation.)")
    else:
        print("[3] skipped: no account_id resolved.")

    # --- 4) option data (read-only) ------------------------------------------
    print("[4] data.instrument.get_option_contracts(AAPL, US_OPTION) ...")
    first_symbol = None
    try:
        res = data.instrument.get_option_contracts(
            category="US_OPTION", underlying_symbols="AAPL", page_size=5)
        body = _as_json(res)
        print("    RESPONSE:")
        print(_indent(json.dumps(body, indent=2, default=str)))
        rows = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(rows, list) and rows:
            first_symbol = rows[0].get("symbol") or rows[0].get("option_symbol")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print("    (Option-chain read failed - likely no OPRA/option data")
        print("     entitlement on this app key. Live option quotes need this.)")

    # --- 5) option snapshot + FRESHNESS probe --------------------------------
    # Measures the delay between the quote's own timestamp and 'now' so you can
    # tell empirically whether this environment gives real-time or ~15-min data.
    if first_symbol:
        print(f"[5] data.option_market_data.get_option_snapshot({first_symbol}) ...")
        try:
            res = data.option_market_data.get_option_snapshot(
                first_symbol, category="US_OPTION")
            body = _as_json(res)
            print("    RESPONSE:")
            print(_indent(json.dumps(body, indent=2, default=str)))
            # Webull reports the feed delay directly via `delay_minutes` - this
            # is the authoritative real-time-vs-delayed signal (age can be large
            # simply because the market is closed).
            delay = _find_delay_minutes(body)
            if delay is not None:
                verdict = "REAL-TIME" if delay == 0 else f"DELAYED by {delay} min"
                print(f"    >>> delay_minutes = {delay}  =>  {verdict}")
            now = datetime.now(UTC)
            stamps = _find_timestamps(body)
            if stamps:
                print("    --- quote timestamps (field -> age vs now) ---")
                print("    (age is inflated when the market is closed; trust")
                print("     delay_minutes above for the real-time verdict)")
                for path, dt in stamps:
                    age = now - dt
                    secs = age.total_seconds()
                    print(f"      {path:<28} {dt.isoformat()}  age={_fmt_age(secs)}")
            else:
                print("    (no timestamp-like field found)")
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            print("    (Snapshot failed - option market-data entitlement likely")
            print("     missing on this key/environment.)")
    else:
        print("[5] skipped: no option symbol captured from step 4.")

    print("-" * 64)
    print("DONE. If you saw an account_id above, live auth + account access work.")
    print("Freshness verdict comes from delay_minutes in the snapshot: 0 => real-")
    print("time; 15 => delayed feed (typical of the OpenAPI sandbox, not usable")
    print("for a real-time exit ladder). Timestamp ages are only meaningful while")
    print("the market is open.")
    return 0


# --- freshness helpers -------------------------------------------------------
def _find_timestamps(obj, path: str = "") -> list:
    """Recursively find timestamp-like fields and parse them to aware datetimes.

    Handles epoch seconds/millis and common ISO-8601 strings. Returns
    [(field_path, datetime_utc), ...].
    """
    found: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else str(k)
            kl = str(k).lower()
            if isinstance(v, (dict, list)):
                found.extend(_find_timestamps(v, kp))
            elif any(t in kl for t in ("time", "stamp", "ts", "date")):
                dt = _parse_dt(v)
                if dt is not None:
                    found.append((kp, dt))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_find_timestamps(v, f"{path}[{i}]"))
    return found


def _parse_dt(v):
    from datetime import datetime as _dt
    # epoch (int/str of digits): seconds or milliseconds
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            n = float(v)
            if n > 1e12:      # milliseconds
                n /= 1000.0
            if 1e9 < n < 4e9:  # plausible epoch seconds (2001..2096)
                return _dt.fromtimestamp(n, tz=UTC)
            return None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    # ISO-8601 string
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = _dt.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _fmt_age(secs: float) -> str:
    secs = abs(secs)
    if secs < 90:
        return f"{secs:.0f}s"
    return f"{secs/60:.1f}m"


def _age_label(secs: float) -> str:
    a = abs(secs)
    if a <= 120:
        return "<- REAL-TIME"
    if 12 * 60 <= a <= 18 * 60:
        return "<- ~15-min DELAYED"
    return "<- delayed"


def _find_delay_minutes(obj):
    """Return the first `delay_minutes`-style value found, else None."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("delay_minutes", "delayminutes", "delay"):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
            found = _find_delay_minutes(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_delay_minutes(v)
            if found is not None:
                return found
    return None


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
