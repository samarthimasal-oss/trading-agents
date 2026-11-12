# TradingAgents for MetaTrader 5

A multi-agent autonomous trading system for your MetaTrader 5 account.
Quant agents propose trades, a **Kronos foundation model** forecasts price
paths, an AI committee (Gemini) reviews them, a risk agent sizes and filters
them, an executor places and manages them, and a guardian watches over the
whole account. A local web dashboard gives you the live picture.

**Current focus: XAUUSD (gold), intraday.** XAUUSD is the default primary
symbol; FX pairs remain available as secondary instruments. The project is
being upgraded in phases toward five independent strategies, validated ML,
an independent risk governor and a native MQL5 EA. Read the plan before
running anything:

- [`PROJECT_AUDIT.md`](PROJECT_AUDIT.md) — verified findings and P0/P1/P2 backlog
- [`EXISTING_ARCHITECTURE.md`](EXISTING_ARCHITECTURE.md) — how the current system works
- [`UPGRADE_ROADMAP.md`](UPGRADE_ROADMAP.md) — phased plan and acceptance tests
- [`REPOSITORY_EVALUATION.md`](REPOSITORY_EVALUATION.md) — reference-repo reuse decisions and licenses

**Phase 1 is complete on branch `upgrade/phase-1-foundation`:** the backtest
import collision is fixed, guardian lockouts survive restarts, sizing and
stops use the last **closed** bar, quotes are quarantined per symbol, config
is validated at startup, and the foundation `pytest` suite is green.

**Phase 2 implementation is complete on branch `upgrade/phase-2-strategies`:** a closed-bar
market-data engine, causal feature layer, market-structure detectors, five
independent strategy engines, a deterministic arbiter, and a conservative
research replay with per-strategy metrics. The engines are **research-only**:
live execution still uses the legacy quant/Kronos path until the new engines
pass out-of-sample validation. See section 6.

**Phase 3 native EA implementation is complete on the same branch:** an MQL5
Expert Advisor with native five-strategy signals, broker-side SL/TP, risk
sizing, partial/breakeven/trailing/time exits, idempotent intents, halt/resume,
reconciliation and Strategy Tester reports. It compiles with zero errors and
zero warnings and runs in the tester without Python. The Python↔EA PAPER
smoke test passed on 2026-11-11 (dry-run ack, duplicate rejection,
bridge-restart reconnect, zero-mismatch reconciliation). Three in-sample
tester passes lost money (**-18.66R** over three months, PF 0.77), so the EA
stays PAPER/dry-run by default and is not authorised for live trading.
Reports: [`docs/reports/`](docs/reports/README.md). Protocol:
[`docs/PROTOCOL.md`](docs/PROTOCOL.md). See section 7.

```
                       +-----------------------+
  MT5 market data ---> |  Quant agents         |  trend / momentum / reversion
                       |  + breakout / pullback|  / breakout / pullback
                       +----------+------------+
                                  v  consensus (strength + confidence)
                       +-----------------------+
  H1/H4/D1 Kronos ---> |  Kronos agent         |  neural forecast (direction,
  M15  forecast  --->  |                       |  move-in-ATR, path sanity)
                       +----------+------------+
                                  v
                       +-----------------------+
                       |  LLM committee (AI)   |  approve / veto / resize
                       +----------+------------+
                                  v
                       +-----------------------+
                       |  Risk agent           |  sizing, spread, hours, news
                       +----------+------------+  gates, dead-market guard
                       +-----------------------+
                       |  Executor             |  --> MT5 terminal API
                       |  (orders, SL/TP,      |
                       |   breakeven, trailing,|
                       |   Kronos exit-timing) |
                       +----------+------------+
                                  ^  override
                       +-----------------------+
                       |  Guardian             |  daily-loss + max-drawdown
                       |  (account supervisor) |  -> flatten + lockout
                       +-----------------------+
```

## Run the dashboard

```powershell
python dashboard.py          # opens http://localhost:5005 in your browser
```

Dashboard highlights:

- **LIVE / DRY-RUN / STOPPED** badge, MT5 connection state, auto-refresh
  countdown + wall-clock pills
- **LIVE FEED ticker** — real broker events (fills, closes, SL moves,
  partial closes, rejects) streamed from the execution journal, with flash
  animation on new events
- Equity / balance / today P&L / floating P&L cards
- **Risk guard gauges** — day-loss budget and peak-drawdown bars
- **Market analysis chart** with timeframe tabs (M15/H1/H4/D1), EMA overlays,
  volume bars, and the **Kronos forecast path** drawn after the last candle
- **Multi-timeframe outlook chips** per symbol (clickable → jumps the chart)
- **Trade Plan card** — for every symbol, the next trade at a glance:
  entry / stop / target / lots / risk $ / R:R / blockers, plus the H1–D1
  confluence chips
- **Kronos accuracy card** — live hit-rate dial, per-timeframe split,
  recent graded calls, and "unanimous vs split ensemble" comparison
- **Today's committee funnel** — filled / vetoed / risk-blocked / flat
- Equity curve, strategy desk, decision journal, live log
- Buttons: Start DRY-RUN, Start LIVE, Stop, Reset guardian

## 1. Install

```powershell
cd trading_agents
pip install -r requirements.txt
copy config.example.json config.json    # then edit your copy
python main.py check
```

`config.json` is **not committed** (gitignored) — it holds your private keys.
The `vendors/Kronos` folder (NeoQuasar/Kronos, Apache-2.0) ships with the
repo; model weights download automatically from Hugging Face on first use.

At startup the config is validated against `core/config_schema.py`. A missing
or out-of-range value stops the process with a readable list of every problem
(exit code 2) instead of failing with a `KeyError` mid-cycle. Quotes are also
checked before use: a zero, crossed, stale or too-wide feed is labelled
`QUARANTINED` and cannot open new risk.

## 2. MetaTrader 5 terminal

- Install **MetaTrader 5** (the desktop terminal) and log in.
- **Strongly recommended: use a DEMO account first.** Leave `mt5.login`
  as `0` and `mt5.password` empty to auto-attach to the running terminal,
  or fill them to force a specific account.
- `main.py check` verifies the connection.

## 3. Keys (LLM, optional Telegram)

- `llm.api_key` — a Gemini API key (https://aistudio.google.com/apikey).
  Alternatively set `llm.api_key_file` to a JSON file containing
  `{"gemini_api_key": "..."}`. If missing/invalid the system runs in
  **quant+Kronos-only mode** (all decisions rule-based); nothing crashes.
- `telegram.bot_token` (or `telegram.token_file`) — optional phone alerts
  from **@BotFather**: send `/newbot`, paste the token, and the notifier
  reports fills / closes / flatten-all automatically. It auto-discovers
  your `chat_id` via `getUpdates` after you send the bot any message.

## 4. News filter & dead-market guard

- `news.enabled` — blocks new entries around high-impact calendar events
  (ForexFactory JSON) for the affected currency ±`window_before_min` /
  `window_after_min` minutes; the dashboard shows a 📰 chip when active.
- `risk.dead_market_atr_ratio` — if ATR goes dead vs that symbol's average
  range, entries are paused and the signal board shows the regime as `dead`.

## 5. Multi-timeframe forecasts (Kronos v2)

`kronos.timeframes` (default `["H1","H4","D1"]`) makes the runner forecast
each additional timeframe **only when that timeframe's bar closes** — cached
to disk (`state/kronos_tf_cache.json`) so restarts keep the current forecast
and its grading history. The M15 forecast stays the execution-weighted one;
higher timeframes feed the committee, the dashboard outlook strip, the trade
plan chips, and a **±`htf_confluence` nudge** on the M15 consensus (support
strengthens a signal, opposition weakens it, and a signal that falls back
below `consensus_threshold` goes flat).

- `ensemble_runs` — independent forecast passes per timeframe (default 3).
  The median path is used; `agreement` is the share of runs that voted the
  same direction. A split ensemble **abstains** below `min_agreement`
  (default 0.6) and confidence is scaled by agreement.
- **Accuracy scorecard** — every forecast is graded against the next closed
  bar: direction hit, realized %, and average error. Results are stored in
  `state/kronos_score.json` and shown on the dashboard (overall + per
  timeframe + "unanimous vs split" form), so you can see whether Kronos is
  earning its vote weight.

## 6. Five independent strategy engines (research)

`market/` turns MT5 bars into a strictly closed-bar `BarSeries`, adds causal
features (`ema20/50/200`, RSI, ATR, ADX, MACD, Bollinger, ROC, volume z-score,
session, previous-day levels, VWAP), and detects structure (confirmed swings,
BOS/CHoCH, liquidity sweeps, fair-value gaps, order blocks, premium/discount).
`strategies/` contains five independent engines:

| Engine | Idea | Default sessions |
|---|---|---|
| `trend` | EMA20/50/200 stack + ADX/ROC + pullback | London / overlap / NY |
| `breakout` | Asia or London range break with compression + volume | London / overlap / NY |
| `smc` | Liquidity sweep + displacement + FVG/order block in discount/premium | London / overlap / NY |
| `momentum` | MACD/ROC acceleration with RSI in the healthy middle | London / overlap / NY |
| `mean_reversion` | Quiet range + band extreme + turn, target the mid-band | London / overlap / NY |

An engine returns a `StrategySignal` proposal, never an order.
`strategies/arbiter.py` drops expired/low-confidence/low-R proposals, keeps
one proposal per strategy, de-duplicates setup ids, blocks simultaneous
long/short exposure unless configured otherwise, and caps proposals per bar
with an explicit rejection reason.

The replay harness is deliberately conservative:

```powershell
python main.py strategies --csv demo_1min.csv          # all five engines
python main.py strategies --csv demo_1min.csv --only trend
python main.py strategies --bars 5000                  # live MT5 history
```

```text
signal at close of bar i -> fill at open of bar i+1
same bar touches stop and target -> stop is assumed first
gap through stop -> fill at the gap open, not the stop
costs -> configured spread + slippage on entry and exit
```

It reports trades, win rate, total/expectancy R, profit factor, max drawdown,
sample Sharpe, MFE/MAE, exit reasons, and session/regime/month breakdowns.
The default replay allows one concurrent position, which keeps the sample near
the 2-3 trades/day design target instead of stacking correlated entries.
`demo_1min.csv` is synthetic FX-like fixture data, **not** an edge claim. The
CLI infers an FX-like point/digits/spread scale for it; override with
`--point`, `--digits`, `--spread-points`, and `--slippage-points` for other
instruments.

Configuration lives in `strategies`: `enabled`, `min_confidence`,
`max_signals_per_bar`, `allow_opposite_directions`, `min_rr`, `weights`,
per-engine `engines.<name>` blocks, and a `replay` block. The engines are not
yet wired into the live executor; `signal_mode=engines` is a later phase.

## 7. Native MQL5 EA (PAPER by default)

`mql5/` contains the Expert Advisor and its modules:

```text
mql5/Experts/TradingAgents/TradingAgentsEA.mq5   entry point
mql5/Include/TradingAgents/*.mqh                 JSON, protocol, socket,
                                                 features, signals, risk,
                                                 state, trade manager, report
```

Install (Windows, MT5 installed):

```powershell
# copy/compile into the MT5 data folder (default: auto-detect)
powershell -ExecutionPolicy Bypass -File scripts\install_ea.ps1 -Compile

# create the loopback token; also writes the MT5 common-files copy
python main.py ea-token

# start the Python bridge, then attach TradingAgentsEA to XAUUSD M15
python main.py ea-server
```

Before the first attach, allow loopback sockets in the terminal:

1. **Tools → Options → Expert Advisors**.
2. Enable **Allow WebRequest for listed URL**.
3. Add `127.0.0.1` (host only, no scheme or port; this is the address passed to
   `SocketConnect`). On build 6316, adding only `http://127.0.0.1:8765` did
   not authorize `SocketConnect`; the host-only row is required.
4. Double-click the empty row below the last entry, type `127.0.0.1`, press
   **Enter**, then click **OK**. The entry is active immediately, but build
   6316 does not persist it across a clean restart.

MT5 stores the list encrypted in `common.ini`; hand-editing the file with a
plaintext URL has no effect, and a startup INI with
`[Experts] WebRequestUrl=127.0.0.1` did not seed the list either. In testing,
the UI-added host-only entry worked immediately but did not survive a clean
terminal restart and `common.ini` was not rewritten. Re-add `127.0.0.1` after
every terminal restart. `SocketConnect` fails with **4014** when the host is
not allowed, and with **5272** when the whitelist is fine but the bridge is
down.

EA inputs of interest:

| Input | Default | Meaning |
|---|---|---|
| `InpMode` | `PAPER` | `PAPER` validates intents and sends nothing; `LIVE` needs a fresh Python heartbeat; `TESTER` uses native signals only |
| `InpUseNativeSignals` | `true` | native five-strategy engine on closed bars |
| `InpNativeInLive` | `false` | require explicit opt-in before native entries in LIVE |
| `InpRiskPercent` | `0.50` | risk per trade (% equity) |
| `InpMaxTotalLots` | `0.30` | exposure cap; valid orders are reduced, not rejected |
| `InpMaxSpreadPoints` | `80` | XAUUSD spread cap |
| `InpTokenFile` | `TradingAgents\token.txt` | token relative to MT5 common files |
| `InpDisconnectGraceSec` | `30` | block new entries after Python goes stale |

Safety model:

- Python sends authenticated, newline-delimited JSON intents with an
  idempotency key; the EA re-validates symbol, price, stops, spread, session,
  margin, volume and exposure before any order.
- The EA persists seen keys, position state and daily equity, so a restart does
  not duplicate a trade. A timed-out intent becomes `UNKNOWN` and must be
  reconciled, never retried blindly.
- `PAPER` returns `dry_run` acks. `LIVE` requires `InpMode=LIVE` plus a live
  heartbeat. If Python disappears, existing positions keep their broker-side
  SL/TP and management rules; new entries stop after the grace period.
- No martingale, grid or uncontrolled averaging exists in the EA.
- Sockets are unavailable in the Strategy Tester, so `TESTER` mode never waits
  for Python.

Honest tester evidence: three in-sample XAUUSD M15 passes lost money
(202 trades, -18.66R, PF 0.77 over 2026-08-01 → 2026-11-01; 65 trades,
-14.33R on the one-month check). The EA is therefore **not** promoted to live
execution. Full reports, caveats and the sizing bug found during the pass are in
[`docs/reports/README.md`](docs/reports/README.md).

## 8. Test before going live (in this order)

```powershell
python main.py backtest --csv demo_1min.csv --bars 500   # legacy quant replay
python main.py strategies --csv demo_1min.csv           # five-engine research report
python main.py run --dry-run               # full live pipeline, NO real orders
python -m pytest -q                        # 110-test regression suite
```

For the native EA, also verify the bridge before attaching it:

```powershell
python main.py ea-token
python main.py ea-server --seconds 30      # expect a clean bind/stop, no token error
python scripts\ea_live_smoke.py            # PAPER-only; run during session hours
```

`ea_live_smoke.py` sends a PAPER intent, verifies the `dry_run` and duplicate
acks, restarts the bridge and reconciles. Run it during London/overlap/New
York hours; outside those it prints `SKIP` and exits 2. It refuses to run
unless the EA reports `mode=PAPER`.

Then:

```powershell
python main.py run                         # live, MT5 terminal must be connected
python main.py status                      # account, positions, journal
```

Research tooling lives in `research/`: `backtest.py` (full replay engine),
`tune.py` (weight grid search), `horizon_edges.py` (per-agent horizon sweeps).
The top-level `backtest.py` remains the CLI facade. `demo_1min.csv` is a
synthetic fixture for determinism tests, **not** a performance claim.

## 9. What it does on your account (per poll cycle)

- Pulls current prices, spread, equity/margin.
- Manages open positions: **partial profit at 1R** (books 50% by default,
  then forces the stop to breakeven), breakeven lock + ATR trailing stop,
  an optional **time stop** (`risk.time_stop_minutes`, default off), and the
  Kronos exit-watch which closes positions early when the forecast is
  **against** the position for `exit_watch.persistence` consecutive bars.
- Once per closed bar: runs the quant agents + Kronos ensemble forecast,
  builds a weighted consensus (with H1/H4/D1 confluence), on non-zero
  consensus the AI committee reviews, the risk agent gates + sizes, and
  market orders go out with ATR-based SL/TP.
- Every event is journaled to `state/agents.sqlite` (decisions, fills,
  closes, SL modifications, partial closes) and rendered on the dashboard.
  Initial risk per ticket is persisted in `state/executor_state.json` so a
  restart still knows where 1R is.

## 10. Risk knobs (defaults)

| Knob | Default | Meaning |
|---|---|---|
| `risk_percent_per_trade` | 0.5 | equity % risked per trade (ATR-sized) |
| `max_positions` / `max_open_same_symbol` | 2 / 1 | concurrency limits |
| `max_daily_loss_percent` | 3.0 | day loss → flatten + rest of day |
| `max_equity_drawdown_percent` | 15.0 | peak-to-now drop → flatten + lockout |
| `max_spread_points` | 40 | global spread ceiling in MT5 points |
| `max_spread_points_by_symbol` | XAUUSD 80, FX 20 | per-symbol override; XAUUSD is wider than FX |
| `trading_hours_utc` | [0, 24] | session restriction |
| `breakeven_atr` / `trailing_atr` | 1.0 / 1.0 | stop management |
| `partial_close_enabled` | true | book profit at 1R |
| `partial_close_at_r` / `partial_close_pct` | 1.0 / 50 | trigger and size of the partial |
| `time_stop_minutes` | 0 (off) | close positions that never worked out |

## 11. Agents section (tuning)

- `ema_fast/ema_slow` — trend crossover
- `rsi_*` + roc thresholds — momentum
- `weight_*` + `consensus_threshold` — weighted committee math
  (Kronos weigh-in included: `weight_kronos`)
- `atr_sl_mult/atr_tp_mult` — stops in ATR multiples

## 12. Disclaimers

Trading CFDs/FX carries significant risk of loss. Bugs, broker differences,
and market gaps can all produce outcomes different from the backtest — and
backtests cannot replay the live LLM/Kronos committees. This software is
provided as-is, without warranty; **you** are responsible for every order
it places on your account. Test on demo first.
