# Project Audit — XAUUSD AI Trading System

**Repository:** `samarthimasal-oss/trading-agents` (private)
**Audited commit:** `ffd2848f0bfe506e3d339ab5d62d9fc0cd362149` (`main`)
**Audit date:** 2026-11-02
**Auditor role:** senior quant / MQL5 / backend / frontend review
**Backup:** git tag `pre-upgrade-2026-11-02`, folder `backups/2026-11-02/` (config + state, git-ignored)

---

## 0. Executive summary (plain language)

The project is a **working Python trading prototype**, not yet the system described in the
upgrade brief. It connects to MetaTrader 5, reads live XAUUSD/EURUSD/USDJPY prices, runs five
indicator voters plus a Kronos forecast, optionally asks Gemini to review a trade, sizes it,
and can place/manage orders. The dashboard is a single Flask page that shows real account,
chart, position, forecast and scorecard data.

It is **not** yet:

- five independent strategy engines (the current five "agents" are votes inside one
  consensus score);
- a native MQL5 EA (there is no `.mq5` file anywhere in the project);
- a React/FastAPI application (dashboard is a 73 KB Flask file with embedded HTML/JS);
- an ML system (no XGBoost/LightGBM, no trained models, no model registry);
- a learning system (the database stores decisions and events, not closed-trade outcomes);
- covered by automated tests (there is one `selftest.py`, and it currently fails).

Three defects are **release-blocking** because they undermine trust in results or risk:

1. **Backtesting is broken.** A Python package named `backtest/` shadows the working
   `backtest.py` module. `python selftest.py` fails, and `python main.py backtest` loads the
   wrong file. Any performance number produced today is either from the older engine or from
   `backtest/backtest.py`, not from the CLI the README documents.
2. **Daily risk lockout does not survive a restart.** Guardian lockout and day-start equity
   live only in memory, so restarting the runner (or the dashboard's Start button) resets the
   daily loss budget and the lockout.
3. **Risk sizing and stop distance use the unfinished bar's ATR.** Signals correctly use
   closed bars, but `ctx.atr` (used for position size, stop-loss and take-profit) is read from
   the last row of the full dataframe, which includes the forming candle.

The good news: the execution path, MT5 wrapper, partial profit, breakeven, trailing, spread
guards, Kronos ensemble and dashboard charts are real and worth preserving. The upgrade should
**extend this codebase**, not replace it.

**Recommended first move:** fix the three P0 defects and add a test harness before adding any
new strategy. A larger system built on an unverifiable backtest and a resettable risk lockout
would be less safe, not more advanced.

---

## 1. Scope and method

### 1.1 What was inspected

- Every Python source file in the repository (4,853 lines excluding `vendors/`, `backups/`
  and `__pycache__`).
- All configuration keys in `config.json` and `config.example.json` (secrets not copied into
  this document).
- The live SQLite journal schema and row counts.
- Runtime state files under `state/`.
- Git history, `.gitignore`, dependency list, README.
- The four reference repositories: metadata, README/SPEC, recursive file trees, and selected
  source modules (see `REPOSITORY_EVALUATION.md`).

### 1.2 What was executed

| Check | Command | Result |
|---|---|---|
| MT5 package + terminal + account | `python main.py check` | **Pass** — demo login `5057915388`, equity/balance ≈ 100,149.78 |
| Symbol feeds | same | EURUSD OK, USDJPY OK, XAUUSD OK (bid 4012.79 / ask 4013.04), **GBPUSD invalid** (bid 0.0, spread ≈ 133,170 pts) |
| LLM key presence | same | Key found (Gemini), model label `gemini-3.5-flash...` |
| Import resolution | `python -c "import backtest"` | **Fail** — resolves to `backtest/__init__.py`, no `run` |
| Self-test | `python selftest.py` | **Fail** — `ImportError: cannot import name 'run' from 'backtest'` |
| Dependencies | `importlib.util.find_spec` | MetaTrader5, pandas, numpy, flask, requests, torch, fastapi, sklearn, pytest present; **xgboost and lightgbm absent** |
| Dashboard bind | source inspection | `127.0.0.1` only, no authentication |

No orders were placed during the audit. No configuration, database, dataset or key was
modified. The backup was taken before any change.

### 1.3 Limitations

- The audit cannot judge profitability. No out-of-sample, cost-inclusive, point-in-time
  result exists yet, and the backtest that could produce one is broken.
- MT5 Strategy Tester was not run because no MQL5 EA exists.
- GBPUSD's invalid feed is a broker/terminal condition, not a code defect; the code correctly
  rejects it at the risk gate, but the dashboard still renders the raw values.
- Telegram was not tested end-to-end because no bot token is configured.

---

## 2. Current state at a glance

| Dimension | Current state | Target state | Gap |
|---|---|---|---|
| Language | Python 3, MQL5 absent | Python + MQL5 | **No EA** |
| UI | Flask monolith, embedded HTML/JS | React + TS + Tailwind + FastAPI + WS | **Full rebuild** |
| Instruments | 4 FX/metals symbols, EURUSD default | XAUUSD primary | Config change + focus |
| Timeframes | M15 primary; Kronos H1/H4/D1 | M1–D1, closed-bar discipline | Partial; ATR uses forming bar |
| Strategies | 5 indicator voters + Kronos vote | 5 independent engines with own exits/SL/TP | **Not independent** |
| ML | None | XGBoost/LightGBM, regime, meta-labelling | **Missing** |
| AI committee | One Gemini reviewer | 7 specialist roles, debate, decision | **Minimal** |
| Risk | ATR sizing, spread/hours/position/day/DD gates | Independent governor, persisted, configurable | Partial; not persisted |
| Execution | Python market orders, SL/TP, partial, BE, trail, time stop | MQL5-native + Python brain, protocol | Python-only |
| Backtest | Two engines, CLI broken | Research + MT5 tester, walk-forward, costs | **Broken/duplicated** |
| Learning | Decisions/events only | Trade outcome store + loss taxonomy | **Missing** |
| Tests | One failing self-test | Unit/integration/E2E | **Missing** |
| Docs | README + this audit set | Per-subsystem docs | Partial |

### 2.1 File inventory (source)

| Area | Files | Lines (approx.) | Notes |
|---|---|---|---|
| Entry point | `main.py` | 290 | check / run / backtest / status / testorder |
| Orchestration | `runner.py` | 607 | per-symbol cycle, Kronos cache, live board |
| Agents | `agents/quant.py`, `kronos_agent.py`, `llm_agent.py`, `risk_agent.py` | 900 | voters, forecast, Gemini review, sizing |
| Core | `core/mt5_client.py`, `models.py`, `state_store.py`, `news_filter.py`, `notifier.py`, `kronos_score.py` | 800 | MT5 wrapper, dataclasses, SQLite, calendar, Telegram, scorecard |
| Execution | `execution/executor.py`, `guardian.py` | 520 | orders, partial/BE/trail/time stop, account kill switch |
| Backtest | `backtest.py` + `backtest/` package | 1,000+ | **duplicate implementations** |
| Dashboard | `dashboard.py` | 1,428 | Flask + embedded page + 4 APIs |
| Tests | `selftest.py` | 78 | broken import |
| Vendors | `vendors/Kronos` | external | model code vendored |

Dependencies declared: `MetaTrader5`, `pandas`, `numpy`, `requests`, `flask`. This is a
remarkably small footprint and should be preserved as the base layer. New dependencies must
be justified one at a time.

---

## 3. What already works (preserve)

These are real, verified capabilities. The upgrade must not regress them.

### 3.1 MT5 connectivity and symbol handling

`core/mt5_client.py` initialises the terminal, logs in with configured credentials, resolves
broker-specific symbol suffixes, prepares symbols, reads ticks and bars, computes daily bot
P/L by magic number, and normalises symbol settings (point, digits, tick value, tick size,
volume min/max/step, stops level). It has explicit error types and a shutdown path.
**Verified live.**

### 3.2 Closed-bar signal timing

`runner.py` acts once per new bar (`forming_bar_time == self._last_bar_time[symbol]` guard),
then builds signals from `closed_df = df.iloc[:-1]`. This is the correct shape and should be
kept. The defect is narrower than "look-ahead everywhere": only `ctx.atr`/`ctx.rsi` for risk
sizing are taken from the forming bar.

### 3.3 Execution and position management

`execution/executor.py` implements:

- market orders with SL/TP, filling-mode fallback, retcode handling and retries;
- partial profit at a configurable R multiple (default 1R, 50%);
- breakeven lock and ATR trailing with a step threshold;
- optional time stop;
- close-on-opposite-signal;
- quote sanity checks before managing positions (zero/crossed/stale feed);
- per-ticket state persisted in `state/executor_state.json`, with an SQLite event fallback
  for partial closes.

This is more than a prototype. It is the natural seed for the MQL5 position manager.

### 3.4 Risk gates that already exist

`agents/risk_agent.py` checks, in order: invalid quote, maximum spread, maximum positions per
symbol, maximum total positions, trading hours (UTC), cooldown between same-symbol entries,
margin level, equity, and ATR availability. Sizing uses `trade_tick_value`, `trade_tick_size`,
`volume_min/max/step` and a notional leverage cap, then rounds **down** to the volume step.
It also applies the LLM's resize multiplier, capped at 1.0 (the LLM can reduce, never increase,
size).

### 3.5 Guardian account protection

`execution/guardian.py` enforces an equity floor, peak-to-equity maximum drawdown (peak
persisted in SQLite), and a daily bot P/L limit, with flatten + lockout on breach. The
drawdown peak survives restarts. The daily lockout does not (see P0-2).

### 3.6 Kronos integration

- Lazy-loaded, failure-safe: if torch/model download/inference fails, it returns `{}` and the
  system continues.
- Multi-run ensemble with median path, direction agreement, move-in-ATR, out-of-distribution
  flag and path sanity limits.
- H1/H4/D1 forecasts cached per closed bar and persisted to disk across restarts.
- HTF confluence is applied as a bounded adjustment, not as sole authority.
- A scorecard grades each forecast against the next closed bar, per timeframe and by
  agreement bucket, and the dashboard renders it.

This is a genuine differentiator and matches the brief's requirement to preserve and improve
Kronos.

### 3.7 Dashboard data plane

The Flask page displays account balance/equity/floating P/L, live positions, trade plans,
candles with EMAs, Kronos forecast overlays, scorecard, event ticker and log tail, with
auto-refresh. The APIs `/api/snapshot`, `/api/chart`, `/api/start`, `/api/stop`,
`/api/reset_guardian` are real. The UI is visually strong (glass v3), even though the
architecture is not maintainable at 73 KB in one file.

### 3.8 Safety behaviours worth keeping

- Dry-run is the default in the dashboard Start control.
- The LLM is optional; missing key/network/malformed JSON degrades to quant-only.
- The news guard fails open (trading continues) rather than freezing the system.
- Telegram failures are logged, never propagated into the loop.
- Broker/terminal failures are caught and surfaced.
- The dashboard binds to localhost only.

---

## 4. Confirmed defects (P0 — fix before new features)

### P0-1 — `backtest` package shadows the `backtest` module; CLI and self-test are broken

**Evidence**

```
> python -c "import backtest; print(backtest.__file__); print(hasattr(backtest,'run'))"
...\trading_agents\backtest\__init__.py
False

> python selftest.py
ImportError: cannot import name 'run' from 'backtest'
(...\trading_agents\backtest\__init__.py)
```

**Cause**

The repository contains both `backtest.py` (top-level replay engine with `run`, `load_csv`,
`fetch_bars_mt5`, `print_result`) and a package directory `backtest/` whose `__init__.py` is
empty. Python resolves the package first. `main.py cmd_backtest` does `import backtest as bt`
and therefore cannot find `run`; `selftest.py` does `from backtest import run` and fails at
import time.

**Impact**

- The documented `python main.py backtest` command does not work.
- The self-test cannot run at all.
- Two backtest implementations coexist with different assumptions (fills, costs, tuning),
  so any result quoted from one may not describe the other.
- There is no trustworthy performance baseline for the upgrade.

**Required fix**

Choose **one** engine, then make the import explicit. Recommended: rename the package to
`research/` (or `btengine/`), keep `backtest.py` as the CLI facade, and have it call the fuller
engine. Delete nothing until equivalence is demonstrated. Add a regression test that imports
the CLI path and runs a short CSV backtest. Full acceptance criteria are in
`UPGRADE_ROADMAP.md` Phase 1.

### P0-2 — Guardian daily lockout and day-start equity are memory-only

**Evidence**

`execution/guardian.py`:

```python
self._halted_day: str | None = None
self.day_start_equity: float | None = None
...
def _lockout_until_tomorrow(self, why):
    self._halted_day = self.utc_day()
    ...
def reset(self):
    self._halted_day = None
```

No read/write of these fields to SQLite or JSON. `__init__` sets them to `None` on every
process start.

**Impact**

A restart after a daily-loss breach clears the lockout immediately. The dashboard Start
button launches a fresh process, so a user can inadvertently bypass the daily loss limit by
clicking Stop then Start. Day-start equity also resets, so the loss budget is measured from
the post-restart equity rather than the true start-of-day equity.

**Required fix**

Persist `{utc_day, day_start_equity, halted_day, halt_reason, halted_at}` in a small
`guardian_state` table or JSON file using atomic replace. On startup, load it and:

- if `halted_day == today`, remain halted;
- if the stored day is older, roll the day and set `day_start_equity` from the first observed
  equity of the new day;
- manual reset must require an explicit confirmation token and must be audited;
- add tests that simulate a restart mid-lockout and assert trading stays blocked.

### P0-3 — Risk sizing, SL and TP use the unfinished bar's ATR

**Evidence**

`runner.py`:

```python
df = quant.context_with_indicators(ctx, bars, a)   # bars includes forming bar
...
closed_df = df.iloc[:-1]                            # signals use closed bars
...
plan = self.risk.build_plan(ctx, closed_df, a, consensus, review, account)
```

`agents/quant.py`:

```python
row = latest(df)                 # last row = forming bar
ctx.atr = float(row["atr"])
```

`agents/risk_agent.py`:

```python
atr_val = ctx.atr or 0.0
stop_dist = max(atr_val * sl_mult, ...)
tp_dist = atr_val * tp_mult
volume = self.size_position(ctx, info, account.equity, atr_val)
```

**Impact**

The position size, stop distance and take-profit distance can change during the bar as the
forming candle moves. This violates the brief's closed-candle rule, makes execution
non-deterministic relative to the logged signal, and makes live results diverge from a
backtest that uses closed-bar ATR. It is a correctness issue even though it is not a
future-data leak in the strict sense.

**Required fix**

Set `ctx.atr` and `ctx.rsi` from `closed_df` after slicing, or pass the closed-bar ATR
explicitly into `build_plan`/`size_position` and stop reading `ctx.atr` there. Add a test with
a deliberately extreme forming bar and assert the plan's SL/TP/volume match the closed-bar
ATR.

### P0-4 — GBPUSD feed is invalid and can pollute displays/decisions

**Evidence**

`python main.py check` reports `bid=0.0 ask=1.3317 spread=133170.0pts`. The risk gate
correctly rejects quotes where `bid <= 0`, but `main.py check`, the dashboard snapshot and
the chart endpoint still show the raw values. A future strategy that consumes the quote
before the risk gate could compute nonsense.

**Impact**

Broker/terminal feed issue, not a code bug, but the system should **quarantine** a symbol
whose quote fails sanity checks and label it clearly, rather than treating it as a normal
market. This prevents accidental trading, bad chart scaling and misleading dashboard tiles.

**Required fix**

Add a `QuoteQuality` check at the data layer (`bid > 0`, `ask > 0`, `ask >= bid`,
`spread <= hard_max`, freshness within N seconds). Return a structured `quarantined` flag
with a reason. All consumers (strategies, dashboard, backtest data fetch) must honour it.
Do not auto-close positions on a bad quote; log and skip management only.

---

## 5. High-priority gaps (P1)

### P1-1 — No configuration validation or schema/versioning

`config.json` is loaded with `json.load` and accessed by direct key indexing throughout
(`cfg["risk"]`, `cfg["agents"]["ema_slow"]`, ...). A missing key raises `KeyError` deep in
the loop; a wrong type can silently produce bad sizing. There is no schema, no version field,
no startup validation, no migration path, and no separation between secrets and preferences.

**Fix:** define a typed schema (Pydantic v2 or a small dataclass validator), validate at
startup with human-readable errors, add `config_version`, and keep secrets in a separate
git-ignored file or environment variable. Never log secret values.

### P1-2 — No trade-outcome store and no learning loop

The SQLite schema has only `decisions`, `events` and `equity_log`. There is no table for
closed trades, MFE/MAE, realized R, exit reason, spread/slippage at entry and exit, regime,
committee reasoning, or loss classification. The dashboard can show open positions but cannot
answer "which strategy loses money and why".

**Fix:** add `trades`, `trade_events`, `model_registry`, `strategy_runs` and `daily_stats`
tables with a migration runner. Reconcile fills from MT5 deal history, not from in-memory
assumptions. Backfill existing history where possible.

### P1-3 — No native MQL5 EA, no protocol, no reconciliation

Execution is entirely Python. If Python stops, open positions keep their broker-side SL/TP
(if set) but lose breakeven/trailing/partial management and time stops. There is no
heartbeat, no duplicate-order prevention across restarts, no ack/retry protocol, and no
position reconciliation. The brief requires an EA that can run in the Strategy Tester without
Python. None exists.

**Fix:** build a deterministic MQL5 EA that owns order placement and position management,
with a documented localhost JSON protocol (auth token, sequence numbers, idempotency keys,
heartbeats, acks, restart recovery, reconciliation). Python sends *intents*, never raw
orders; the EA enforces risk limits independently. This is Phase 3 and the largest single
work item.

### P1-4 — No tests, and the one existing test fails

There are no unit, integration or E2E tests. `selftest.py` covers a random synthetic series
and a Kronos-disabled path at best, and currently cannot import. The P0-1 defect is exactly
the kind of regression a two-line test would have caught.

**Fix:** add `pytest`, split tests by layer, and require the following before any phase is
called complete:

- config validation (valid/invalid/missing/secret redaction);
- indicator correctness against known values;
- closed-bar discipline (no forming bar in signals or risk);
- risk gate matrix (spread, hours, positions, cooldown, margin, quote quarantine);
- sizing with real contract specs and volume-step rounding;
- guardian lockout persistence across a simulated restart;
- backtest determinism on a fixed CSV;
- executor order-check/retcode paths with a fake MT5 client;
- API contract tests for every dashboard endpoint;
- one end-to-end dry-run cycle on synthetic data.

### P1-5 — Dashboard is a monolith with almost no controls

`dashboard.py` is 73 KB, with the entire page embedded as a Python raw string. Only four API
endpoints exist. There is no strategy enable/disable, no risk-parameter editing, no backtest
launch, no report export, no log viewer beyond a tail, no emergency close-all button, no
settings page, no committee page, no learning page. The brief requires every control to
perform a real backend action. Today most controls do not exist.

**Fix:** Phase 7 builds the React/FastAPI app against a versioned API. Keep the Flask page as
a read-only fallback until parity is proven, then retire it. Do not add more HTML strings to
`dashboard.py` in the meantime.

### P1-6 — The "five agents" are not five independent strategies

`agents/quant.py` defines `TrendAgent`, `MomentumAgent`, `ReversionAgent`, `BreakoutAgent`,
`PullbackAgent`. Each returns a `Signal`; `build_consensus` sums weighted direction and
confidence into one consensus. They share one dataframe, one ATR, one SL/TP multiplier, and
one exit path. They cannot hold independent positions, have no strategy-specific exits, and
their performance is not tracked separately.

The brief requires five independent engines (trend, breakout, SMC/ICT, momentum, mean
reversion) with their own entry, exit, SL, TP, R:R and performance. The existing voters are
useful **inputs** to a trend/momentum engine, but they are not the target architecture.

### P1-7 — Risk governor is not independent and lacks required controls

Current controls: risk %, spread, max positions, per-symbol positions, hours, cooldown,
daily loss, max drawdown, notional leverage, margin level. Missing:

- persisted daily lockout and day-start equity (P0-2);
- maximum consecutive losses;
- maximum exposure across the account (notional and margin);
- maximum slippage enforcement at fill time;
- per-strategy risk budgets and correlation caps;
- an auditable emergency close-all path independent of the AI;
- explicit "reduce-only" semantics for risk interventions;
- a manual kill switch that survives restart.

The risk code is also coupled to `MT5Client` and `MarketContext`; the MQL5 EA will need its
own independent enforcement, so the rules must be expressed once in a broker-agnostic policy
module and mirrored (not duplicated ad hoc) in MQL5.

### P1-8 — Backtest methodology is not yet research-grade

`backtest.py` is a simple bar replay with next-bar fills and SL-before-TP. `backtest/backtest.py`
is fuller (spread/costs, ambiguous-bar rule, signal cache, tuning split). Neither currently
provides:

- multi-fold walk-forward with purge/embargo;
- out-of-sample holdout that is evaluated once;
- parameter-stability surfaces;
- Monte Carlo trade-sequence analysis;
- deflated Sharpe / probability of backtest overfitting;
- session and strategy breakdowns;
- commission and swap modelling;
- point-in-time macro/news data;
- a machine-readable report with provenance (data hash, config hash, code version).

`tune.py` uses a single 60/40 split and writes the winner to `config.json` with a `.bak`.
That is a useful prototype, not a validated promotion process.

### P1-9 — No structured logging, correlation IDs or health checks

Logging is standard `logging` to file + SQLite events. There is no JSON log option, no
correlation ID tying a signal to an order to a fill to a close, no `/health` endpoint, no
process watchdog, no heartbeat between components, and no automatic restart policy. The
dashboard's runner state is a PID file plus `tasklist`, which is fragile.

### P1-10 — Dependency and environment reproducibility

`requirements.txt` has five unpinned lower bounds (`>=`). Kronos requires torch and model
downloads that are not declared. ML dependencies are absent. There is no lockfile, no virtual
environment bootstrap, no Python version check, and no CI. A fresh machine may not reproduce
the current behaviour.

---

## 6. Medium/low priority findings (P2)

| ID | Finding | Impact | Recommendation |
|---|---|---|---|
| P2-1 | `config.json` defaults to EURUSD and four symbols; XAUUSD is not primary | Wrong focus, split attention | Make XAUUSD primary, others opt-in |
| P2-2 | `max_spread_points: 40` is broker-specific and tiny for XAUUSD if points are 0.01 (40 pts = $0.40); verify against live 25 pts | Could block or allow wrong spreads | Express spread limit in price/ATR terms with a points override per symbol |
| P2-3 | `time_stop_minutes: 0` disables time exits | Positions can linger in dead markets | Set a sensible default after research |
| P2-4 | `trading_hours_utc: [0, 24]` allows all hours including rollover | Wider exposure than intended | Add session windows per strategy |
| P2-5 | No maximum consecutive-loss protection | Tilt/regime-break risk | Add configurable streak limit + cooldown |
| P2-6 | No partial-fill/partial-close reconciliation against deal history | Journal can diverge from broker | Add deal-based reconciliation |
| P2-7 | `main.py check` prints raw quotes without quarantine labels | Misleading | Use the quote-quality object |
| P2-8 | `dashboard.py` embeds secrets-adjacent config values (model name only; no key) | Low | Keep; ensure no key ever reaches the page |
| P2-9 | No CSRF/auth on localhost APIs | Any local process can start/stop trading | Add a local API token + origin check |
| P2-10 | `testorder` places a real order with a hard-coded heuristic | Dangerous if run by mistake | Require `--confirm`, refuse on live account, log loudly |
| P2-11 | `notifier` auto-detects Telegram chat from `getUpdates` | Fine, but token handling is plaintext in config | Move token to env/keyring; redact logs |
| P2-12 | News guard only covers ForexFactory high-impact events; no surprise/actual data | Limited context | Add event outcomes post-release, point-in-time |
| P2-13 | No timezone/DST policy documented | Session logic can drift | Centralise broker/server/UTC conversion |
| P2-14 | Kronos grade is next-bar direction only; no calibration or error distribution | Weak model monitoring | Add Brier score, MAE by horizon, calibration curve |
| P2-15 | `vendors/Kronos` is a large vendored dependency with no version pin | Supply-chain/repro risk | Record commit hash and license; pin |
| P2-16 | No `.env.example`, no secrets documentation | Onboarding risk | Add sanitized example + docs |
| P2-17 | `state/` grows without retention policy (`live.json` 31 KB, logs 78 KB, scorecard capped) | Slow dashboard over months | Add retention/vacuum jobs |
| P2-18 | No database migrations; schema is `CREATE TABLE IF NOT EXISTS` | Future schema changes will be manual | Add a migration runner now |
| P2-19 | `demo_1min.csv` is a tiny sample with no provenance | Risk of mistaking it for research data | Label as fixture only; keep real data separate |
| P2-20 | README architecture claims exceed implementation (e.g. "multi-agent" implies more than one LLM) | Expectation mismatch | Align docs with reality; this audit is the start |

---

## 7. Risk and safety assessment

### 7.1 What is safe today

- No live trading occurs without an explicit `run` (dashboard defaults to dry-run).
- The LLM can veto or resize down; it cannot increase size or bypass the risk agent.
- The risk agent rejects invalid quotes, wide spreads, position limits, off-hours and
  cooldown.
- The guardian flattens on equity floor, peak drawdown and daily loss.
- Broker-side SL/TP are attached to orders, so a Python crash does not leave positions
  completely unprotected (assuming the broker accepts the SL/TP).
- Kronos, LLM, news and Telegram failures do not stop the loop.

### 7.2 What is not safe enough

| Risk | Scenario | Consequence | Mitigation |
|---|---|---|---|
| Daily lockout reset | Restart after loss | Continued trading past limit | P0-2 persistent state |
| Forming-bar ATR | Volatile bar | Oversized position or too-tight stop | P0-3 closed-bar ATR |
| Duplicate orders | Timeout + retry across restart | Double position | Idempotency keys + broker reconciliation |
| Orphaned positions | Python dies while position open | No BE/trail/time exit | MQL5 EA owns management |
| Stale/invalid quote | Broker feed glitch | Bad management decisions | Quote quarantine + fail-closed for management |
| Config typo | Missing/negative risk value | Zero or huge size | Schema validation + hard bounds |
| Local API abuse | Any local process calls `/api/start` | Unauthorized live trading | Local auth token + confirm live |
| Backtest illusion | Broken CLI / optimistic fills | False confidence | Fix engine + costs + walk-forward |
| Overfitting | Tuning on test data | Live underperformance | Purge/embargo/holdout/PBO |
| Model drift | Regime change | Silent degradation | Monitoring + retraining + rollback |

### 7.3 Fail-safe policy to adopt

1. **Fail closed for new risk, fail open for exits.** If data or AI is unavailable, do not
   open new trades; still allow protective exits and SL/TP management.
2. **Risk governor has veto power over every signal source**, including ML/AI and manual
   dashboard actions.
3. **Live mode requires two explicit actions**: enable live in settings *and* confirm at
   start, with a persistent audit event. Default is dry-run.
4. **Every order intent has an idempotency key** derived from `(strategy, symbol, bar_time,
   direction)`; the EA rejects duplicates.
5. **Reconcile before trade.** On startup and every N seconds, compare local state to broker
   positions/deals; never assume.
6. **No martingale, grid, averaging down, or lot doubling.** Enforce this in code and tests,
   not just documentation.

---

## 8. Security and secrets

| Item | Status | Action |
|---|---|---|
| `config.json` in git | Ignored, verified | Keep ignored; never print key |
| Gemini key | Present in live config, absent from example | Move to env/keyring; redact logs |
| MT5 password | Empty in current config (terminal session) | Support env/keyring; never commit |
| Telegram token | Empty | Add via env/keyring when enabled |
| Dashboard network exposure | `127.0.0.1` only | Keep; add auth token before any remote access |
| Dashboard APIs | No auth/CSRF | Add local token + same-origin check |
| Logs | May contain account/order details, not keys | Add redaction filter; retention policy |
| Backup folder | Contains `config.json` with key | Git-ignored, local only; document deletion policy |
| Vendored code | Kronos, license/version not recorded | Record commit + license in `THIRD_PARTY.md` |

**Immediate secret hygiene:** confirm `backups/` is ignored (it is not currently listed in
`.gitignore`; add it), and never attach `config.json`, `state/agents.sqlite` or logs to a
commit, issue or chat.

---

## 9. Verification evidence

### 9.1 Runtime

```
19:04:28 INFO mt5_client: Connected to MT5: login=5057915388 server=MetaQuotes-Demo
account 5057915388 equity=100149.78 balance=100149.78 margin_level=0
EURUSD bid=1.1137 ask=1.11376 spread=6.0pts
GBPUSD bid=0.0 ask=1.3317 spread=133170.0pts
USDJPY bid=157.916 ask=157.92 spread=4.0pts
XAUUSD bid=4012.79 ask=4013.04 spread=25.0pts
LLM committee: key found (model=gemini-3.5-flash...)
```

### 9.2 Journal

```
tables: decisions, events, equity_log
decisions: 107
events: 95
equity_log: 643
```

No closed-trade table exists; the journal cannot currently produce win rate, profit factor or
expectancy by strategy.

### 9.3 Import failure

```
ImportError: cannot import name 'run' from 'backtest'
(C:\...\trading_agents\backtest\__init__.py)
```

### 9.4 Dependency check

```
MetaTrader5 True, pandas True, numpy True, flask True, requests True,
torch True, fastapi True, sklearn True, pytest True,
xgboost False, lightgbm False
```

---

## 10. Preservation plan

Before modifying anything:

1. **Git tag** `pre-upgrade-2026-11-02` — done.
2. **Backup folder** `backups/2026-11-02/` containing `config.json` and `state/` — done.
3. **Add `backups/` to `.gitignore`** — pending (P0 hygiene).
4. **Do not delete** `backtest.py`, `backtest/`, `vendors/Kronos`, `dashboard.py` or any state
   until replacement parity is proven.
5. **Do not touch** `config.json` keys or values without writing a `.bak` and a migration
   note.
6. **Do not commit** `config.json`, `state/`, `backups/`, `*.sqlite`, `*.pkl`, logs or keys.
7. **Keep the Flask dashboard runnable** while the React app is built.

Compatibility rule for the upgrade: every phase must leave `python main.py check` working and
must not change the meaning of an existing config key without a migration.

---

## 11. Prioritized backlog

### P0 — before any new feature

| # | Item | Acceptance |
|---|---|---|
| 1 | Fix `backtest` collision; choose one engine | `python selftest.py` passes; `python main.py backtest --csv demo_1min.csv` prints a report |
| 2 | Persist guardian lockout/day-start | Restart mid-lockout stays locked; test proves it |
| 3 | Use closed-bar ATR for SL/TP/size | Test with extreme forming bar shows no change |
| 4 | Quote quarantine | Invalid symbol flagged and excluded everywhere; GBPUSD labelled |
| 5 | Add `backups/` to `.gitignore`; verify no secrets staged | `git status` clean of secrets |

### P1 — foundation for the target system

| # | Item | Acceptance |
|---|---|---|
| 6 | Config schema + startup validation | Bad config fails fast with a readable message |
| 7 | Migration runner + trade/outcome tables | Schema versioned; old DB upgrades cleanly |
| 8 | Test harness (unit/integration/E2E) | CI-style `pytest` green |
| 9 | Structured logging + correlation IDs | One trade traceable end-to-end |
| 10 | Risk policy module + consecutive-loss/exposure/slippage | Rule matrix tested |
| 11 | Five independent strategy engines | Each emits its own signal/exit/SL/TP and is tracked separately |
| 12 | Research backtester v2 | Costs, walk-forward, holdout, Monte Carlo, report |
| 13 | Native MQL5 EA + protocol | Compiles; runs in Strategy Tester; heartbeat/dedup/reconcile |
| 14 | FastAPI + React dashboard | All pages real, all controls wired |
| 15 | ML pipeline | Reproducible train/validate/test; registry; no live control without validation |
| 16 | Committee | 7 roles, timeouts, graceful degradation, logged reasoning |
| 17 | Learning engine | Closed-trade store, loss taxonomy, reports, promotion gate |

### P2 — quality and polish

Items P2-1 … P2-20 in section 6, scheduled after the core system is trustworthy.

---

## 12. Bottom line

The existing project is a **credible prototype with real MT5 execution and a strong
dashboard**, but its research and risk foundations are not yet trustworthy enough to support
five strategies, ML, an AI committee and a native EA. The correct sequence is:

1. make the backtest importable and deterministic;
2. make risk state persistent and closed-bar correct;
3. add the trade-outcome store and tests;
4. then build the five-strategy engine, MQL5 EA, ML, committee and new dashboard.

Skipping steps 1–3 would produce a more impressive-looking system with less reliable results.
The roadmap in `UPGRADE_ROADMAP.md` follows this order and keeps the working prototype alive
at every stage.
