"""HTTP-layer test using FastAPI's in-process TestClient (no server needed).

Validates: health, bad-secret 401, missing-field 400, valid entry (RTH gate
disabled for the test), duplicate suppression, and non-universe rejection.

Run:  python -m scripts.http_test
"""
from __future__ import annotations

import app.main as m
from fastapi.testclient import TestClient

SECRET = m.SECRETS.webhook_secret

# Disable the RTH gate for a deterministic test regardless of wall-clock time.
_orig = m.load_params
def _params_no_rth(*args, **kwargs):
    p = _orig(*args, **kwargs)
    p.setdefault("risk", {})["rth_only"] = False
    p.setdefault("session", {}).pop("eod_flatten_at", None)
    return p
m.load_params = _params_no_rth


def _body(ticker="SPY", action="CALL", price=500.0, t=111, secret=SECRET, signal="ORB_BREAKOUT_UP"):
    return {"secret": secret, "ticker": ticker, "action": action,
            "signal": signal, "price": price, "atr": 0.9, "tf": "5", "time": t}


def run():
    # clean any leftover state from prior runs
    m.STATE.positions.clear()
    m.STATE.daily_pnl = 0.0
    m._SEEN.clear()

    with TestClient(m.app) as c:
        r = c.get("/health"); assert r.status_code == 200, r.text
        print("health:", r.json())

        r = c.post("/webhook", json=_body(secret="wrong"))
        assert r.status_code == 401, r.text
        print("bad-secret ->", r.status_code)

        r = c.post("/webhook", json={"secret": SECRET, "ticker": "SPY"})
        assert r.status_code == 400, r.text
        print("missing-fields ->", r.status_code)

        r = c.post("/webhook", json=_body(t=1001))
        j = r.json(); assert j["accepted"] is True, j
        print("valid ->", j["reason"], "| open:", m.STATE.open_count())

        r = c.post("/webhook", json=_body(t=1001))
        assert r.json()["reason"] == "duplicate", r.json()
        print("duplicate ->", r.json()["reason"])

        r = c.post("/webhook", json=_body(ticker="GME", t=1002))
        assert r.json()["accepted"] is False, r.json()
        print("non-universe ->", r.json()["reason"])

        print("positions:", list(c.get("/positions").json().keys()))
    print("\nHTTP LAYER OK")


if __name__ == "__main__":
    run()
