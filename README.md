# TradingAgents for MetaTrader 5

A multi-agent autonomous trading system for your MetaTrader 5 account.
Quant agents propose trades, a **Kronos foundation model** forecasts price
paths, an AI committee (Gemini) reviews them, a risk agent sizes and filters
them, an executor places and manages them, and a guardian watches over the
whole account. A local web dashboard gives you the live picture.

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
  rejects) streamed from the execution journal, with flash animation on
  new events
- Equity / balance / bigger today P&L / floating P&L cards
- **Risk guard gauges** — day-loss budget and peak-drawdown bars
- **Market analysis chart** with timeframe tabs (M15/H1/H4/D1), EMA overlays,
  volume bars, and the **Kronos forecast path** drawn after the last candle
- **Multi-timeframe outlook chips** per symbol (clickable → jumps the chart)
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

## 5. Multi-timeframe forecasts

`kronos.timeframes` (default `["H1","H4","D1"]`) makes the runner forecast
each additional timeframe **only when that timeframe's bar closes** — cached,
cheap, and each carries its own horizon (H1 ≈ next 16 h, D1 ≈ next 16 days).
The M15 forecast stays the execution-weighted one; higher timeframes feed
the committee and the dashboard outlook strip.

## 6. Test before going live (in this order)

```powershell
python main.py backtest --bars 5000        # replay history, R-multiple stats
python main.py run --dry-run               # full live pipeline, NO real orders
```

Then:

```powershell
python main.py run                         # live, MT5 terminal must be connected
python main.py status                      # account, positions, journal
```

Backtest tooling in `backtest/`: `backtest.py` (replay), `tune.py`
(weight grid search), `horizon_edges.py` (per-agent horizon sweeps).

## 7. What it does on your account (per poll cycle)

- Pulls current prices, spread, equity/margin.
- Manages open positions: breakeven lock + ATR trailing stop; the Kronos
  exit-watch closes positions early when the forecast is **against** the
  position for `exit_watch.persistence` consecutive bars.
- Once per closed bar: runs the quant agents + Kronos forecast, builds a
  weighted consensus; on non-zero consensus the AI committee reviews, the
  risk agent gates + sizes, and market orders go out with ATR-based SL/TP.
- Every event is journaled to `state/agents.sqlite` (decisions, fills,
  closes, SL modifications) and rendered on the dashboard.

## 8. Risk knobs (defaults)

| Knob | Default | Meaning |
|---|---|---|
| `risk_percent_per_trade` | 0.5 | equity % risked per trade (ATR-sized) |
| `max_positions` / `max_open_same_symbol` | 2 / 1 | concurrency limits |
| `max_daily_loss_percent` | 3.0 | day loss → flatten + rest of day |
| `max_equity_drawdown_percent` | 15.0 | peak-to-now drop → flatten + lockout |
| `max_spread_points` | 40 | no entries during panic spreads |
| `trading_hours_utc` | [0, 24] | session restriction |
| `breakeven_atr` / `trailing_atr` | 1.0 / 1.0 | stop management |

## 9. Agents section (tuning)

- `ema_fast/ema_slow` — trend crossover
- `rsi_*` + roc thresholds — momentum
- `weight_*` + `consensus_threshold` — weighted committee math
  (Kronos weigh-in included: `weight_kronos`)
- `atr_sl_mult/atr_tp_mult` — stops in ATR multiples

## 10. Disclaimers

Trading CFDs/FX carries significant risk of loss. Bugs, broker differences,
and market gaps can all produce outcomes different from the backtest — and
backtests cannot replay the live LLM/Kronos committees. This software is
provided as-is, without warranty; **you** are responsible for every order
it places on your account. Test on demo first.
