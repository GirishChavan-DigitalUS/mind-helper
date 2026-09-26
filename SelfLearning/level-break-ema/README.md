# Level Break + EMA Pullback [13 / 48 / 200]

A multi-timeframe intraday indicator for TradingView (Pine Script v6) that flags
high-quality options entries on **SPY / QQQ / IWM**, modeled on the
Team2Trading ("Casey") method: break a key level on the higher timeframe, then
enter on a pullback to the 13 EMA in the direction of the stacked trend.

> This is a discretionary-assist tool. It marks *qualified candidates* — you
> still confirm context (flag quality, news, broad market) before taking a trade.

---

## What it plots

| Element | Meaning |
|---|---|
| **EMA 13** (yellow, auto green/red) | Fast EMA — the pullback/entry line. Tints green in a bull stack, red in a bear stack. |
| **EMA 48** (purple) | Mid EMA — trend "spine" and optional pullback filter. |
| **EMA 200** (red) | Slow EMA — overall bias. |
| **PDH / PDL** | Previous Day High / Low (from the prior daily bar). |
| **PMH / PML** | Pre-Market High / Low (built live 04:00–09:30 ET, then frozen for the session). |
| **`brk` marks** | A 15-minute candle closed beyond a key level (the break was confirmed). |
| **LONG / SHORT triangles** | An entry trigger fired. |
| **`w` circle** | That entry came from a *wick-touch* rejection (vs a reclaim close). |
| **Status table** | Live Trend / Break / Pullback / Session / 13 EMA readout. |

---

## The strategy in 4 steps

1. **Levels** — Mark the 4 key levels: PDH, PDL, PMH, PML.
2. **Trend** — Stack the EMAs:
   - **Bull** → `13 > 48 > 200` (EMAs act as support → buy dips)
   - **Bear** → `200 > 48 > 13` (EMAs act as resistance → sell rips)
3. **Break** — Confirm a level break with a **15-minute candle CLOSE** beyond
   the level (above PMH/PDH for longs, below PML/PDL for shorts).
4. **Entry** — On your entry chart, wait for price to **dip into the 13 EMA**
   inside the stacked trend, then trigger:
   - **A+ CALLS:** 15m close > PMH (or PDH) → dip to 13 EMA → long
   - **A+ PUTS:** 15m close < PML (or PDL) → pullback to 13 EMA → short

---

## How to use it (step by step)

1. Open a **SPY** (or QQQ/IWM) chart on the **2-minute** timeframe.
2. Add the indicator (`casey_level_break_ema.pine`) to the chart.
3. Leave **Confirmation Timeframe = 15**. The break is read from the last
   *closed* 15m candle, so it does **not repaint**.
4. Watch the **Status table** (top-right):
   - **Trend** = BULL / BEAR / MIXED (EMA stack).
   - **Break** = which level was broken and confirmed on the 15m.
   - **Pullback** = a dip to the 13 EMA is currently armed.
   - **Session** = Pre-Mkt / RTH / Closed.
5. Take signals only when **Trend and Break agree** (BULL + Bull break for
   calls, BEAR + Bear break for puts) and price is at the 13 EMA.
6. Set a TradingView **alert** on any of the built-in conditions (see below) so
   you don't have to watch every tick.

### First-time verification (do this once)
- Add it to SPY 2-min and confirm **PMH/PML build during pre-market** and then
  **freeze at 09:30**.
- Reload the chart and confirm break `brk` marks **don't move** (no repaint).
- Toggle **Entry Trigger** and confirm signal timing changes as described below.

---

## Entry triggers

Set **Break & Entry Logic → Entry Trigger**:

- **Reclaim (close cross)** *(default, conservative)*
  Price dips to the 13 EMA to *arm* a pullback, then a later bar **closes back
  across** the 13 EMA (crossover for longs / crossunder for shorts). Confirmation
  first, so it triggers one bar later but with fewer fakeouts.

- **Wick Touch (rejection)** *(aggressive / earlier)*
  A single bar **wicks into** the 13 EMA but **closes back on the trend side**
  (a rejection candle). Fires on that same bar. Only a *fresh* dip triggers
  (the prior bar must have closed on the trend side), which throttles clustered
  hugging bars. Marked with a small **`w`** circle.

- **Both**
  Runs both triggers together.

Related settings:
- **13 EMA Touch Tolerance** — how close a dip must get to count as a touch,
  measured by **ATR × multiple** (default) or a **percent** of price.
- **Require dip to hold beyond the 48 EMA** — long entries must close above the
  48 EMA (mirror for shorts); filters deep pullbacks that break trend.
- **Wick mode: require rejection candle color** — long wick entry must close
  green, short must close red.
- **Only fire entries during Regular Hours** — keeps signals inside 09:30–16:00.

---

## Alerts

Four built-in `alertcondition`s (create via the alarm-clock → *Condition* = this
indicator):

| Alert | Fires when |
|---|---|
| **Long Entry** | A long trigger fires (reclaim or wick). |
| **Short Entry** | A short trigger fires. |
| **Bull Break** | A 15m candle closed above a key level. |
| **Bear Break** | A 15m candle closed below a key level. |

---

## Key settings reference

**Timeframes**
- *Confirmation Timeframe* — HTF whose candle close confirms a break (default 15).

**EMAs**
- Lengths 13 / 48 / 200, colors, and *Tint 13 EMA by trend*.

**Key Levels**
- Toggle PDH/PDL/PMH/PML, colors, line width, labels.
- *Pre-Market Window* (default `0400-0930`) and *Timezone* (`America/New_York`)
  drive PMH/PML — change these if you trade a different session/market.

**Break & Entry Logic**
- Which levels arm a break, touch tolerance, hold-beyond-48 filter, entry
  trigger mode, wick color filter, RTH-only filter.

**Signals & Dashboard**
- Signal colors, break marks, status table position.

---

## Notes & limitations

- **Equities-intraday design.** PMH/PML need a real pre-market session — use on
  SPY/QQQ/IWM (or similar), not 24h instruments.
- **Non-repainting breaks.** The 15m break uses `close[1]` + `lookahead_on`
  (the last *closed* HTF bar), so confirmed breaks are stable on reload.
- **Bull/bear flag quality is not auto-detected.** The trend-stack + pullback
  logic approximates the "buy the flag" idea; you judge the flag yourself.
- Signals are an assist, not a system — position sizing, stops, and exits are
  up to you.
