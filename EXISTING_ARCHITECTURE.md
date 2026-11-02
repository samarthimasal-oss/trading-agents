# Existing Architecture — TradingAgents for MetaTrader 5

**Snapshot:** commit `ffd2848` (2026-11-02), branch `main`
**Purpose of this document:** describe the system *as it exists*, so the upgrade can extend it
without guessing. It is intentionally descriptive, not aspirational. Target architecture is
in `UPGRADE_ROADMAP.md`.

---

## 1. System shape

```
                    ┌──────────────────────────────────────────────┐
                    │              Windows laptop                   │
                    │                                              │
  MetaTrader 5 ────►│  core/mt5_client.py                          │
  terminal          │    connect / symbols / ticks / bars / deals   │
  (demo)            │              │                               │
                    │              ▼                               │
                    │  runner.TradingAgentRunner                   │
                    │    per symbol, once per closed bar           │
                    │      │                                       │
                    │      ├─ quant.context_with_indicators        │
                    │      ├─ quant.AGENTS (5 voters)              │
                    │      ├─ kronos_agent.forecast (M15/H1/H4/D1) │
                    │      ├─ news_filter.NewsGuard                │
                    │      ├─ llm_agent.LlmAgent (Gemini, optional)│
                    │      ├─ risk_agent.RiskAgent                 │
                    │      ├─ guardian.Guardian                    │
                    │      └─ executor.Executor ──────────────────►│──► MT5 orders
                    │              │                               │
                    │              ├─ state_store (SQLite + log)   │
                    │              ├─ state/live.json (board)      │
                    │              └─ state/kronos_*.json          │
                    │                                              │
                    │  dashboard.py (Flask, 127.0.0.1:5005)        │
                    │    /api/snapshot /api/chart /api/start ...   │
                    └──────────────────────────────────────────────┘
```

There is **one process** for the trading loop and **one process** for the dashboard. The
dashboard can spawn/stop the runner as a child process and reads the same files/SQLite
database. There is no message bus, no API layer between runner and dashboard, and no MQL5
component.

---

## 2. Runtime entry points

`main.py` is the CLI:

| Command | What it does | State |
|---|---|---|
| `python main.py check` | Validates MetaTrader5 package, terminal login, account, each configured symbol quote, LLM key presence | Works |
| `python main.py run --dry-run` | Starts the runner without sending orders | Works |
| `python main.py run` | Starts live execution | Works (demo) |
| `python main.py backtest` | Intended replay of the agent stack | **Broken by import collision** |
| `python main.py status` | Prints account, bot positions, journal stats, recent decisions | Works |
| `python main.py testorder` | Places and closes one tiny real order | Works, but dangerous without a confirm flag |

`dashboard.py` is started separately: `python dashboard.py --port 5005` (default binds
`127.0.0.1`). It serves one HTML page and four JSON endpoints.

---

## 3. Module responsibilities

### 3.1 `core/models.py` — shared dataclasses

| Type | Fields (summary) | Used by |
|---|---|---|
| `Candle` | time, open, high, low, close, tick_volume | data layer, indicators |
| `MarketContext` | symbol, timeframe, bars, ema_fast/slow, rsi, atr, bid, ask, spread_points, digits, point | runner, agents, risk |
| `AccountSnapshot` | login, balance, equity, margin, profit, margin_level, currency, daily_pnl | risk, dashboard |
| `Signal` | name, direction, weight, confidence, why | quant voters, Kronos |
| `Consensus` | direction, strength, confidence, signals, reason | runner, risk, LLM |
| `LlmReview` | approved, action, comment, size_multiplier, used_llm, raw | risk, executor |
| `TradePlan` | symbol, action, direction, volume, sl, tp, atr, entry_hint, consensus, review, reason, summary() | executor, dashboard |
| `Position` | ticket, symbol, type, volume, price_open, sl, tp, profit, magic, comment | executor, dashboard |

The models are plain dataclasses with no validation. This is a good lightweight base; the
upgrade should add validation at construction boundaries rather than turning them into a
framework.

### 3.2 `core/mt5_client.py` — broker adapter

Responsibilities:

- `connect()`: `mt5.initialize()` with optional terminal path/login/password/server; raises
  `MT5Error` with the MT5 last error.
- `resolve_symbol(name)`: maps a logical name to a broker's suffixed symbol.
- `prepare(symbol)`: selects the symbol and returns `symbol_info`.
- `symbol_settings(info)`: normalises point, digits, tick value, tick size, volume min/max/
  step, stops level, contract size.
- `tick(symbol)`, `copy_bars(symbol, timeframe, n)`: market data.
- `account()`, `positions(magic)`, `daily_bot_profit(magic)`, `history_deals(...)`.
- `order_check` / `order_send` wrappers with retcode handling and filling-mode fallback.
- `shutdown()`.

Design strengths: explicit errors, no global MT5 state assumptions beyond the library, symbol
resolution handles broker suffixes. Weaknesses: no quote-quality object (P0-4), no
reconnect/heartbeat, no rate limiting, and the adapter is imported directly by risk and
execution rather than injected behind an interface.

### 3.3 `agents/quant.py` — indicators and voters

Pure pandas/numpy, no MT5 dependency, so the same functions run live and in backtests.

Indicators: `to_frame`, `ema`, `rsi`, `atr`, `enrich` (adds EMA fast/slow, RSI, ATR, and
helper columns), `latest`.

Voters (each `analyze(df, cfg) -> Signal`):

| Voter | Logic (summary) | Weight key |
|---|---|---|
| `TrendAgent` | EMA fast vs slow + price location + slope | `weight_trend` |
| `MomentumAgent` | RSI + ROC/EMA impulse | `weight_momentum` |
| `ReversionAgent` | RSI extremes + distance from EMA | `weight_reversion` |
| `BreakoutAgent` | close beyond N-bar high/low with ATR buffer | `weight_breakout` |
| `PullbackAgent` | trend + pullback to fast EMA + rejection candle | `weight_pullback` |

`build_consensus(signals, threshold)`:

```
strength = tanh( Σ weight_i × confidence_i × direction_i )
confidence = Σ(confidence_i × weight_i) / Σ weight_i
direction = 0 unless |strength| ≥ threshold
```

If all active voters agree, the sign is unambiguous; otherwise the sign of the strength
decides. This is a **single consensus**, not five independent strategies. It has no
strategy-specific exits, no per-strategy P/L and no way to run one strategy while another is
disabled.

`context_with_indicators(ctx, bars, cfg)` writes `ema_fast`, `ema_slow`, `rsi`, `atr` back to
the context from `latest(df)` — the **last row, including the forming bar**. This is the
source of P0-3.

### 3.4 `agents/kronos_agent.py` — foundation-model forecast

- `get_predictor(kcfg)`: lazy singleton; inserts `vendors/Kronos` on `sys.path`; imports
  torch, `Kronos`, `KronosTokenizer`, `KronosPredictor`; loads tokenizer + model; selects
  CUDA if available; caches success/failure. Never raises.
- `forecast(df, kcfg, runs)`:
  - trims to `lookback` closed bars, requires ≥ 60;
  - computes the typical bar spacing from the median timestamp difference (avoids weekend
    gaps stretching D1 forecasts);
  - runs `runs` independent sampling passes, each with `sample_count` internal samples;
  - aggregates a median OHLC path;
  - computes direction, move, move-next, agreement, out-of-distribution flag, path sanity
    (`max_path_move_pct`), base timestamp/close;
  - returns `{}` on any failure.
- `direction_from_path`, `summary` helpers produce the text/dict used by the LLM and
  dashboard.

The model is **one extra vote** with weight `weight_kronos` (default 1.1), plus HTF
confluence. It is not the sole authority, which matches the brief. The integration is
genuinely failure-safe.

### 3.5 `agents/llm_agent.py` — Gemini review

- `load_api_key(cfg)`: config key, file, or environment; never logs the value.
- `LlmAgent.review(ctx, consensus, account, positions)`: builds a compact prompt with
  consensus, indicators, recent candles and account state; calls the Gemini
  `generateContent` endpoint; parses JSON into `LlmReview` (`approve` / `veto` / `resize`).
- Failure handling: missing key, timeout, HTTP error, malformed JSON → `LlmReview` with
  `approved=True`, `used_llm=False` (quant-only mode). This means the LLM can only reduce
  risk, never block trading by being unavailable.

Limitations: one model, no specialist roles, no debate, no per-agent evidence, no token/cost
accounting, no caching, no async. The upgrade's committee is a new layer, not a tweak.

### 3.6 `agents/risk_agent.py` — sizing and pre-trade gates

`RiskAgent` holds config + `MT5Client`. Methods:

- `_notional_usd_per_lot(symbol, info, price)`: contract size × price, with FX conversion
  logic for non-USD quote currencies.
- `size_position(ctx, info, equity, atr)`: risk % of equity ÷ (ATR stop distance × tick value
  per lot), then notional leverage cap, then round **down** to volume step and clamp to
  min/max.
- `_round_volume(volume, info)`: floor to step, clamp.
- `pre_trade_gate(ctx, account, open_positions)`: quote sanity, spread limit, per-symbol
  count, total count, UTC hours, cooldown, margin level, equity.
- `build_plan(ctx, df, agent_cfg, consensus, review, account)`: ATR stop/target, entry at
  ask/bid, rounding to digits, size, LLM resize (cap 1.0), zero volume if not approved,
  returns `TradePlan`.

The gate order is sensible. Missing: consecutive-loss limit, account-wide exposure cap,
slippage check at fill, persisted state, per-strategy budget, and a broker-agnostic policy
representation for MQL5 mirroring.

### 3.7 `execution/executor.py` — orders and position management

- `place(plan)`: validates volume/price/SL/TP, computes filling mode, calls order_check then
  order_send with retries, journals `ORDER` events, stores risk state.
- `manage_positions()`: iterates bot positions and applies, per position:
  - quote sanity (skip management on invalid/crossed/stale quotes);
  - partial close at `partial_close_at_r` (default 1R), percentage configurable, recorded in
    executor state and SQLite event to avoid repeats;
  - breakeven at `breakeven_atr`;
  - ATR trailing with `trailing_atr` and `trailing_step_atr`;
  - optional time stop in minutes;
  - SL/TP modification via `order_send` with retcode checks.
- `close(position, reason)`, `close_all(symbol)`, `close_on_opposite(consensus)`.
- State persistence: `state/executor_state.json`, atomic replace; SQLite event fallback for
  partial-close detection.

This is the most mature part of the codebase. It is also the part that must move into MQL5
for tick-level reliability.

### 3.8 `execution/guardian.py` — account supervisor

`Guardian(cfg, client, store)`:

- `utc_day()`: UTC date string.
- `is_halted()`: true only if `_halted_day == today`.
- `check()`:
  1. equity ≤ 0 → lockout + flatten;
  2. peak-to-equity drawdown > `max_equity_drawdown_percent` → lockout + flatten (peak from
     SQLite, survives restarts);
  3. daily bot P/L ≤ −`max_daily_loss_percent` × day-start equity → lockout + flatten.
- `flatten(execute)`: calls `executor.close_all()`.
- `reset()`: clears `_halted_day` (manual dashboard flag).

The daily fields are memory-only (P0-2). `check()` has an implicit `None` return if `magic`
is falsy; the runner sets it, but the function should always return a tuple.

### 3.9 `core/state_store.py` — journal

SQLite schema:

```sql
decisions(ts, symbol, action, direction, volume, sl, tp, strength, confidence,
          llm_action, llm_comment, reason, executed)
events(ts, kind, symbol, detail, retcode, price, volume)
equity_log(ts, equity, balance, margin, floating)
```

Index on `decisions(ts)`. Methods: `decision`, `event`, `equity`, `stats`,
`recent_decisions`, `recent_events`, `peak_equity`, `has_event`, `attach_file_log`,
`close`. Also attaches a rotating file handler to `state/agents.log`.

Limitations: no migrations/versioning, no trade table, no outcome/R metrics, no model or
strategy tables, no retention. `has_event` does a substring match on `detail`, which is
fragile but works for ticket-based partial-close checks.

### 3.10 `core/news_filter.py` — economic calendar

`NewsGuard(cfg)` fetches ForexFactory's weekly JSON, caches to
`state/news_calendar.json` for 6 hours, maps symbols to currencies (XAUUSD → USD), and
returns `(blocked, why)` if a high-impact event is within the configured before/after
window. Any failure returns `(False, "")` — fail-open, so the system keeps trading. A
background refresh is throttled and lock-protected. Only scheduled events are modelled; no
actual/surprise data.

### 3.11 `core/notifier.py` — Telegram

`Notifier(cfg)` reads token from config or token file, auto-detects a chat ID via
`getUpdates` when `chat_id=0`, and sends HTML messages. `trade(kind, detail)` formats
fill/close/reject/SL/guard messages. Failures are logged at WARNING and never propagate.

### 3.12 `core/kronos_score.py` — forecast scorecard

`grade(payload, symbol, timeframe, outcome_close, outcome_ts)` compares the previous
forecast's direction and `move_next` with the realized next-bar move. Stores records in
`state/kronos_score.json` (atomic, capped at 500), deduplicated by `(symbol, tf, base_ts)`.
`summary()` returns overall hit rate, recent form, per-timeframe stats, and an
agreement-split (unanimous vs split). This is a good start; it measures direction only, not
calibration or magnitude error by horizon.

### 3.13 `runner.py` — orchestrator

Per symbol, per poll:

1. `account()` snapshot; attach `daily_pnl`.
2. `guardian.check()`; if halted, flatten and skip.
3. `executor.manage_positions()`.
4. Close on opposite consensus (if enabled).
5. Read the live board and forecasts; compute extra HTF forecasts (cached per closed bar).
6. News guard.
7. New-bar check; only proceed once per forming bar timestamp.
8. `closed_df = df.iloc[:-1]`; minimum bar check.
9. Grade the previous forecast.
10. Build `ctx` (bid/ask/spread/ATR/RSI) and `closed_df` indicators.
11. Build signals: five voters + Kronos (if enabled), consensus, HTF confluence.
12. Optional LLM review.
13. `risk.build_plan`.
14. `executor.place` unless dry-run.
15. Write `state/live.json` board and decision/event rows.
16. Sleep `poll_seconds`.

The runner also loads/saves the Kronos timeframe cache, watches for the dashboard's
`reset_guardian.flg`, handles Telegram notifications, and exposes report data to the
dashboard.

### 3.14 `dashboard.py` — Flask UI

- `load_cfg()` reads `config.json` on every request.
- `get_client()` caches an MT5 client for chart/account reads.
- `get_account_snapshot()` returns account + positions.
- `journal_snapshot()` reads SQLite for decisions/events/equity.
- `live_board()` reads `state/live.json`.
- `trade_plans()` derives plan tiles from the board + account.
- `kronos_score_summary()` reads the scorecard.
- `runner_state()` reads `state/runner.pid`; `start_runner(mode)` spawns
  `main.py run [--dry-run]`; `stop_runner()` uses `taskkill /T /F`.
- `PAGE` is an embedded HTML/CSS/JS document (glass v3) with auto-refresh, canvas chart,
  forecast overlay, plan tiles, scorecard and event ticker.

Endpoints:

| Method | Path | Action |
|---|---|---|
| GET | `/api/snapshot` | account, runner, trading config, strategies, live board, plans, scorecard, journal, log tail |
| GET | `/api/chart?symbol&tf&bars` | OHLC + EMAs + RSI/ATR + Kronos overlay |
| POST | `/api/start` | start runner (dry/live) |
| POST | `/api/stop` | kill runner |
| POST | `/api/reset_guardian` | write reset flag file |

No auth, no CSRF, no WebSocket, no pagination, no export, no strategy/risk controls.

---

## 4. Data flow and timing

### 4.1 Live cycle

```
tick ──► copy_bars(symbol, M15, N)
          │
          ├─ forming bar (last row) ──► ctx.atr / ctx.rsi          [P0-3 defect]
          └─ closed bars (all but last) ──► indicators ──► voters
                                                   │
Kronos(M15) ◄── closed bars ──► forecast ──────────┤
Kronos(H1/H4/D1) ◄── cached per closed HTF bar ────┤
                                                   ▼
                                            consensus
                                                   │
Gemini review (optional) ◄── context ──────────────┤
                                                   ▼
                                            risk plan
                                                   │
                              dry-run? ──yes──► journal only
                                   │no
                                   ▼
                              MT5 order + SL/TP
                                   │
                              manage: partial → BE → trail → time stop
```

### 4.2 Backtest timing (intended)

The fuller engine (`backtest/backtest.py`) documents a strict order:

```
bar i closed
  (1) exits vs high/low using SL/TP as of end of bar i-1
  (2) settle winners
  (3) consensus on bar i
  (4) opposite-close at bar i+1 open
  (5) dead-market gate + entry at bar i+1 open
  (6) BE/trailing updated at bar i close (effective bar i+1)
  (7) tuning sample + equity mark
```

This is the correct shape: decisions on closed bar *i*, fills at *i+1* open, no future data.
The top-level `backtest.py` has similar intent but different assumptions. The collision means
the CLI cannot currently run either predictably.

### 4.3 Timeframes

- Live signals: `trading.timeframe` (default M15).
- Kronos: M15 plus `kronos.timeframes` (H1, H4, D1), each cached per closed HTF bar.
- Dashboard chart: M1/M5/M15/M30/H1/H4/D1/W1/MN1, user-selected.
- There is no common multi-timeframe data engine; each caller fetches its own bars. This will
  be replaced in Phase 2.

---

## 5. Configuration surface

`config.json` (git-ignored) mirrors `config.example.json` plus secrets. Top-level sections:

| Section | Key groups | Notes |
|---|---|---|
| `mt5` | terminal_path, login, password, server, timeout_ms, deviation_points | Credentials; never commit |
| `trading` | symbols, symbol, timeframe, magic, bars_history, poll_seconds, close_on_opposite, require_llm_approval, comment | `symbol` defaults EURUSD; XAUUSD is in the list |
| `agents` | EMA/RSI/ATR periods, SL/TP multiples, five weights, breakout lookback, Kronos weight, consensus threshold | Single shared parameter set |
| `kronos` | enabled, model, tokenizer, device, max_context, lookback, pred_len, T, top_p, sample_count, min_move_atr, max_path_move_pct, ensemble_runs, min_agreement, htf_confluence, timeframes, exit_watch | Real ensemble config |
| `news` | enabled, window_before_min, window_after_min, min_impact | ForexFactory |
| `telegram` | enabled, bot_token, chat_id, token_file, max_len | Token sensitive |
| `risk` | risk %, fixed lot, leverage cap, max positions, same-symbol, daily loss, max DD, spread, BE/trail/step, partial, time stop, dead-market ratio, hours, cooldown | No consecutive-loss or exposure cap |
| `llm` | enabled, provider, api_key, api_key_file, model, timeout, temperature | Secret |
| `logging` | db_file, log_file | Relative paths |

Loading is raw `json.load` with direct indexing. There is no schema, no defaults merge, no
environment override despite a comment in `main.py`, and no version field.

---

## 6. Persistence inventory

| Path | Format | Contents | Git |
|---|---|---|---|
| `state/agents.sqlite` | SQLite | decisions, events, equity_log | ignored |
| `state/agents.log` | text | rotating runtime log | ignored |
| `state/run.log` | text | runner stdout/stderr from dashboard spawn | ignored |
| `state/live.json` | JSON | latest board per symbol: consensus, Kronos, plans, prices | ignored |
| `state/executor_state.json` | JSON | per-ticket risk state, partial flags | ignored |
| `state/kronos_tf_cache.json` | JSON | last forecast per (symbol, tf) | ignored |
| `state/kronos_score.json` | JSON | graded forecast records | ignored |
| `state/news_calendar.json` | JSON | cached events | ignored |
| `state/dashboard.json` | JSON | UI preferences/state | ignored |
| `state/runner.pid` | JSON | runner PID + mode | ignored |
| `state/reset_guardian.flg` | flag | manual guardian reset request | ignored |
| `state/backtest_report.json`, `bt_*.csv`, `bt_sig_*.pkl` | mixed | research artifacts | ignored |
| `backups/2026-11-02/` | copy | pre-upgrade config/state | **must be ignored** |

No file in `state/` is committed. This is correct and must remain true.

---

## 7. Dashboard/runner interaction

```
dashboard process                          runner process
-----------------                          --------------
POST /api/start ──► Popen(main.py run) ──► runner starts
POST /api/stop  ──► taskkill /T /F ─────► runner dies
POST /api/reset_guardian ──► flag file ──► runner polls flag, calls guardian.reset()
GET /api/snapshot ◄── reads live.json + SQLite + MT5
GET /api/chart    ◄── reads MT5 directly
```

This is simple and works, but it has no handshake, no health check, no graceful shutdown, and
no protection against two dashboards or two runners. The PID file can go stale. The next
architecture should replace this with an explicit service supervisor and health endpoint.

---

## 8. Failure behavior (current)

| Failure | Behavior | Assessment |
|---|---|---|
| MT5 terminal not running | `MT5Error` at startup; `check` explains; runner exits | Good |
| Symbol missing | resolve/prepare raises; symbol loop logs and continues | Acceptable |
| Invalid quote | risk gate rejects new trades; management skips | Good, but no quarantine label |
| Kronos unavailable | neutral/no vote; system continues | Good |
| Gemini unavailable | `used_llm=False`, quant-only | Good |
| News feed unavailable | guard returns not blocked | Good (fail-open) |
| Telegram unavailable | warning only | Good |
| SQLite locked/corrupt | not explicitly handled; may raise | Needs hardening |
| Runner crash | positions keep broker SL/TP; BE/trail stops | **Gap** → MQL5 EA |
| Duplicate send after timeout | possible duplicate position | **Gap** → idempotency + reconcile |
| Config missing key | `KeyError` mid-cycle | **Gap** → schema |
| Dashboard access by local process | allowed | **Gap** → token |
| Restart after daily loss | lockout cleared | **Gap** → persistence |

---

## 9. Architectural strengths to preserve

1. **Pure indicator functions** shared by live and backtest — keep this separation.
2. **Executor position management** (partial/BE/trail/time stop) — port the rules to MQL5,
   keep the Python implementation as a fallback/paper path.
3. **Failure-safe optional AI** — keep the "AI unavailable does not stop trading" policy.
4. **Kronos as an ensemble, not an oracle** — keep and improve.
5. **Local-first, small dependency footprint** — resist framework sprawl.
6. **Dashboard visual language (glass v3)** — reuse the design tokens in the React app.
7. **Journaling of decisions and events** — extend, do not replace.

## 10. Architectural weaknesses to remove

1. Single-consensus "five agents" masquerading as five strategies.
2. Two backtest engines and a package/module collision.
3. Monolithic Flask page with no real control surface.
4. Python-only execution with no native EA, heartbeat or reconciliation.
5. Memory-only risk state.
6. No trade-outcome data model, therefore no learning.
7. No ML/model lifecycle.
8. No config schema, migrations, tests or structured logs.
9. Direct coupling from risk/execution to `MT5Client` and global config dicts.
10. No explicit separation between *signal*, *decision*, *order intent* and *fill*.

---

## 11. Extension points for the upgrade

| New component | Integrates at | Constraint |
|---|---|---|
| Market data engine | replaces ad-hoc `copy_bars` calls | must serve M1–D1 with closed-bar semantics and a single clock |
| Five strategy engines | `runner` signal stage | each returns a typed `StrategySignal` with entry/SL/TP/exit/expiry |
| ML pipeline | after features, before committee | causal features, versioned models, validation gate |
| Committee | after strategies/ML, before risk | async, timeouts, no direct orders |
| Risk governor | between committee and execution | independent, persisted, broker-agnostic policy |
| MQL5 EA | execution boundary | owns orders/management; Python sends intents |
| Trade journal | executor/EA events | deal-based reconciliation, MFE/MAE, taxonomy |
| FastAPI | replaces Flask APIs | versioned REST + WebSocket, same SQLite initially |
| React app | replaces embedded page | keeps design language, every control wired |
| Backtester v2 | replaces both current engines | one engine, costs, walk-forward, holdout, reports |

---

## 12. Invariants for every future change

1. Signals are computed only from **closed** bars; risk and execution use the same snapshot.
2. The risk governor can only **reduce** risk and can veto any signal source.
3. AI/ML can never send an order or bypass risk.
4. Live mode is never the default and requires explicit confirmation.
5. Every order intent is idempotent and reconcilable against the broker.
6. `config.json`, `state/`, `backups/`, keys and databases are never committed.
7. `python main.py check` must keep working after every phase.
8. No feature is "done" without a test and a failure-mode description.
