Prompt: Build a TradingView Opening Range Breakout (ORB) Indicator in Pine Script v6

Build a professional TradingView indicator (Pine Script v6) that implements a multi-confluence Opening Range Breakout system. Requirements:

1. Opening Range (OR) Definition

Input dropdown to select OR duration: 5, 15, or 30 minutes.
Draw the OR high and OR low as horizontal lines/box for the selected window, starting at the regular session open.
Today-only logic: Plot only for the current/most-recent trading session. If today is a weekend or market holiday (no session data), fall back to the previous valid trading session. Do not render OR levels on historical days.
2. Breakout Confirmation Highlight

Detect when price crosses above OR high or below OR low (the "crossing candle").
If the next (second) candle continues in the same direction and closes beyond the crossing candle's high (for longs) / low (for shorts), highlight that confirming candle in golden/gold color.
3. Pullback Re-Entry

After a breakout, if price pulls back and touches the OR level again, then re-attempts a breakout in the same direction with the same 2-candle confirmation → apply the same golden highlight to the confirming candle.
4. ORB Reversal / Failed Breakout

Detect breakout rejections (price pokes beyond OR then closes back inside and reverses). Mark these as reversal signals with a distinct color/shape so failed breakouts are visually separated from valid ones.
5. Confluence Layer (act as an expert — combine additional ORB strategies)

Integrate VWAP: confirm long breakouts when price is above VWAP and short breakouts when below. Flag or grade signals by VWAP alignment.
Integrate volume: require above-average volume (e.g., volume > moving average of volume) on the breakout/confirmation candle to validate the signal.
Combine these into a simple confluence score/grade (e.g., A/B/C or star rating) shown per signal, factoring: 2-candle confirmation + VWAP alignment + volume surge.
Research and blend in other proven ORB techniques (e.g., retest entries, first 5-min range within a 30-min range, ATR-based stop/target suggestions) and explain the rationale in code comments.
6. Inputs & UX

Group inputs (OR length, session timezone, colors, volume MA length, toggles for VWAP/volume filters, show/hide reversals).
Provide alertcondition() calls for: initial breakout confirmation, pullback re-entry, and reversal.
Add on-chart labels and a small status table (OR high/low, current bias, confluence grade).
Deliverables

Clean, commented Pine Script v6 code, using request.security / session bounds correctly, no repainting on confirmed signals, and clear separation of each module (OR, breakout, pullback, reversal, confluence).
A short explanation of design choices and any assumptions.