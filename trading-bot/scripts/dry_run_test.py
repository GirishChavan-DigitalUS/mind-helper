"""End-to-end dry-run test of the entry + exit logic (no network, no pytest).

Simulates: an entry alert -> 10-lot entry, then walks the option premium up
through TP1 (+30%), TP2 (+50%), and back down to trigger the 10% trailing stop
on the runners. Also checks the stop-loss path and the spread gate.

Run:  python -m scripts.dry_run_test
"""
from __future__ import annotations

from app.config import load_params, load_secrets
from app.models import Signal
from app.position_manager import ExitEngine
from app.state import StateStore
from app.strategy import Strategy
from app.webull_client import WebullClient


def _fresh():
    secrets = load_secrets()
    state = StateStore(path=_tmp_state())
    client = WebullClient(secrets)
    client.dry_run = True   # force dry-run for the test regardless of .env
    client.authenticate()
    strat = Strategy(client, state)
    engine = ExitEngine(client, state, params_provider=load_params)
    return client, state, strat, engine


def _tmp_state():
    import tempfile
    from pathlib import Path
    return Path(tempfile.gettempdir()) / "tvwb_test_state.json"


def _params_no_rth():
    p = load_params()
    p.setdefault("risk", {})["rth_only"] = False  # allow entry any time in test
    p.setdefault("session", {}).pop("eod_flatten_at", None)
    return p


def test_full_scale_out():
    client, state, strat, engine = _fresh()
    state.positions.clear()
    state.daily_pnl = 0.0
    params = _params_no_rth()

    sig = Signal(ticker="SPY", action="CALL", signal="ORB_BREAKOUT_UP", price=500.0)
    res = strat.handle_signal(sig, params)
    assert res.accepted, res.reason
    pos = state.get("SPY")
    assert pos.contracts_open == 10, pos.contracts_open
    basis = pos.basis
    osym = pos.option_symbol
    print(f"entered 10 @ basis {basis:.2f}")

    # +35% -> TP1 sells 4
    client.set_sim_price(osym, basis * 1.35)
    engine.run_once(params)
    assert state.get("SPY").contracts_open == 6, state.get("SPY").contracts_open
    assert state.get("SPY").tp1_done

    # +55% -> TP2 sells 4 (leaves 2 runners)
    client.set_sim_price(osym, basis * 1.55)
    engine.run_once(params)
    assert state.get("SPY").contracts_open == 2, state.get("SPY").contracts_open
    assert state.get("SPY").tp2_done

    # push peak to +80%, then pull back >10% off peak -> trailing closes runners
    client.set_sim_price(osym, basis * 1.80)
    engine.run_once(params)
    peak = state.get("SPY").peak
    client.set_sim_price(osym, peak * 0.88)   # 12% off peak
    engine.run_once(params)
    assert not state.has("SPY"), "runners should be flat after trailing stop"
    print(f"full scale-out complete; daily_pnl={state.daily_pnl:.2f}")
    assert state.daily_pnl > 0


def test_stop_loss():
    client, state, strat, engine = _fresh()
    state.positions.clear()
    state.daily_pnl = 0.0
    params = _params_no_rth()

    sig = Signal(ticker="QQQ", action="PUT", signal="VWAP_SELL", price=430.0)
    assert strat.handle_signal(sig, params).accepted
    pos = state.get("QQQ")
    client.set_sim_price(pos.option_symbol, pos.basis * 0.75)  # -25%
    engine.run_once(params)
    assert not state.has("QQQ"), "stop-loss should flatten the position"
    assert state.daily_pnl < 0
    print(f"stop-loss OK; daily_pnl={state.daily_pnl:.2f}")


def test_spread_gate():
    _client, state, strat, _engine = _fresh()
    state.positions.clear()
    params = _params_no_rth()
    params["entry"]["max_spread_pct"] = 1.0  # force rejection (sim spread ~3%)
    sig = Signal(ticker="IWM", action="CALL", signal="EMA_CONT_LONG", price=200.0)
    res = strat.handle_signal(sig, params)
    assert not res.accepted and "spread" in res.reason
    print(f"spread gate OK: {res.reason}")


def test_breakeven_stop():
    client, state, strat, engine = _fresh()
    state.positions.clear()
    state.daily_pnl = 0.0
    params = _params_no_rth()

    sig = Signal(ticker="AAPL", action="CALL", signal="ORB_BREAKOUT_UP", price=190.0)
    assert strat.handle_signal(sig, params).accepted
    pos = state.get("AAPL")
    basis, osym = pos.basis, pos.option_symbol

    # +22% arms breakeven (no scale-out yet since TP1 is 30%)
    client.set_sim_price(osym, basis * 1.22)
    engine.run_once(params)
    assert state.get("AAPL").be_armed, "breakeven should arm at +20%"
    assert state.get("AAPL").contracts_open == 10

    # dip below basis -> breakeven stop closes all (small slippage loss, not -20%)
    client.set_sim_price(osym, basis * 0.97)
    engine.run_once(params)
    assert not state.has("AAPL"), "breakeven stop should flatten"
    assert state.daily_pnl > -0.20 * basis * 10 * 100, "loss should be near breakeven, not full stop"
    print(f"breakeven stop OK; daily_pnl={state.daily_pnl:.2f}")


def test_trailing_after_tp1_no_tp2():
    client, state, strat, engine = _fresh()
    state.positions.clear()
    state.daily_pnl = 0.0
    params = _params_no_rth()

    sig = Signal(ticker="NVDA", action="CALL", signal="VWAP_BUY", price=120.0)
    assert strat.handle_signal(sig, params).accepted
    pos = state.get("NVDA")
    basis, osym = pos.basis, pos.option_symbol

    client.set_sim_price(osym, basis * 1.35)   # +35% -> TP1 sells 4, trailing arms
    engine.run_once(params)
    assert state.get("NVDA").contracts_open == 6
    assert state.get("NVDA").tp1_done and not state.get("NVDA").tp2_done

    client.set_sim_price(osym, basis * 1.45)   # peak up, still < TP2 (+50%)
    engine.run_once(params)
    peak = state.get("NVDA").peak

    client.set_sim_price(osym, peak * 0.88)    # 12% off peak -> trailing closes the 6
    engine.run_once(params)
    assert not state.has("NVDA"), "trailing should close remainder after TP1 without TP2"
    print(f"trailing-after-TP1 OK; daily_pnl={state.daily_pnl:.2f}")


if __name__ == "__main__":
    test_full_scale_out()
    test_stop_loss()
    test_spread_gate()
    test_breakeven_stop()
    test_trailing_after_tp1_no_tp2()
    print("\nALL DRY-RUN TESTS PASSED")
