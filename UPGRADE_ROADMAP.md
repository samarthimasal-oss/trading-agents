# Upgrade Roadmap — Autonomous Hybrid XAUUSD System

**Baseline:** `ffd2848` (2026-11-02)
**Companion documents:** `PROJECT_AUDIT.md`, `EXISTING_ARCHITECTURE.md`, `REPOSITORY_EVALUATION.md`
**Mode:** extend the working prototype; never replace it wholesale.

---

## 0. Guiding principles

1. **Trust before intelligence.** A verified backtest and persistent risk state come before
   five strategies, ML or a committee.
2. **One engine, one truth.** Signals, risk and execution must use the same closed-bar
   snapshot; research and live must share the same rules.
3. **Risk is independent.** No model, agent, dashboard button or EA signal can bypass the
   risk governor.
4. **AI is optional and bounded.** The system trades safely with no LLM, no Kronos and no
   network.
5. **Fail closed for new risk, fail open for exits.** Data/AI failure blocks entries, never
   protective exits.
6. **Every control does something real.** No decorative buttons, no simulated metrics.
7. **No fabricated performance.** If a result cannot be produced from real data with costs
   and out-of-sample separation, it is not reported.
8. **Small, reversible steps.** Each phase leaves the system runnable and tested.

### 0.1 What "success" means

The target is **not** a promised return. It is a system that can answer, with evidence:

- Does each strategy have a positive expectancy after spread, commission, slippage and swap?
- Is that edge stable across walk-forward folds, regimes and sessions?
- Does the risk governor keep drawdown within the configured budget in every tested path?
- Does the EA behave identically in the Strategy Tester and in demo?
- Can the system survive restarts, disconnects, duplicate sends and missing AI?

The brief's "2–3 legitimate opportunities per day" is a **frequency target**, not a profit
target. The research may conclude that some strategies should trade less, or not at all.
Aurum's honest result — no demonstrated edge on 14 years of XAUUSD, with mean reversion and
opening-range breakouts losing after costs — is a warning, not a template for claims.

---

## 1. Target architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Windows laptop                                   │
│                                                                             │
│  ┌──────────────────────┐        ┌───────────────────────────────────────┐  │
│  │  React + TypeScript  │◄──WS──►│  FastAPI service                      │  │
│  │  Tailwind dashboard  │  REST  │   /api/v1/*  /ws                      │  │
│  └──────────────────────┘        │   auth (local token), schemas, errors │  │
│                                  └───────────────┬───────────────────────┘  │
│                                                  │ reads/writes             │
│                                  ┌───────────────▼───────────────────────┐  │
│                                  │  SQLite (WAL) → PostgreSQL-ready      │  │
│                                  │  trades, signals, decisions, models,  │  │
│                                  │  equity, events, migrations           │  │
│                                  └───────────────┬───────────────────────┘  │
│                                                  │                         │
│  ┌───────────────────────────────────────────────▼───────────────────────┐  │
│  │                     Trading core (Python)                             │  │
│  │                                                                       │  │
│  │  MarketDataEngine ──► FeatureStore ──► StrategyEngine ×5              │  │
│  │       │                    │                 │                        │  │
│  │       │                    │                 ├─ Trend                 │  │
│  │       │                    │                 ├─ Breakout              │  │
│  │       │                    │                 ├─ SMC/ICT               │  │
│  │       │                    │                 ├─ Momentum              │  │
│  │       │                    │                 └─ Mean reversion        │  │
│  │       │                    │                 │                        │  │
│  │       │              ML pipeline (XGB/LGBM, regime, meta-label)       │  │
│  │       │                    │                 │                        │  │
│  │       │              Kronos ensemble (M15/H1/H4/D1, scored)           │  │
│  │       │                    │                 │                        │  │
│  │       │              AI committee (7 roles, async, bounded)            │  │
│  │       │                    │                 │                        │  │
│  │       │              Risk governor (independent, persisted)           │  │
│  │       │                    │                 │                        │  │
│  │       │              Order intent + idempotency                       │  │
│  │       │                    │                 │                        │  │
│  │       └──────────────► Trade journal / learning engine ◄──────────────┘  │
│  └───────────────────────────────────────────────┬───────────────────────┘  │
│                                                  │ localhost JSON protocol  │
│                                  ┌───────────────▼───────────────────────┐  │
│                                  │  Native MQL5 EA (MT5 terminal)        │  │
│                                  │  auth, heartbeat, dedup, risk mirror, │  │
│                                  │  orders, SL/TP, partial, BE, trail,   │  │
│                                  │  time exit, reconcile, StrategyTester │  │
│                                  └───────────────┬───────────────────────┘  │
│                                                  │                         │
│                                  ┌───────────────▼───────────────────────┐  │
│                                  │  MetaTrader 5 (demo first)            │  │
│                                  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Key boundaries

| Boundary | Contract |
|---|---|
| Market data → strategies | closed bars only, one clock, typed `BarSeries` per timeframe |
| Strategies → committee | `StrategySignal` (entry, SL, TP, R, expiry, evidence) |
| Committee → risk | `TradeProposal` (never an order) |
| Risk → execution | `ApprovedOrder` with size, SL/TP, idempotency key, expiry |
| Python → EA | authenticated JSON intent + ack; EA re-validates |
| EA → broker | native orders and management |
| Everything → journal | immutable events with correlation ID |

### 1.2 Non-goals

- No martingale, grid, hedge basket, unlimited averaging down or lot doubling.
- No mandatory paid API, no Ollama, no cloud VPS requirement.
- No RL in the live decision path until it beats a rule-based baseline out-of-sample.
- No replacement of the working MT5 adapter or executor until parity is proven.
- No claim of profitability without a documented out-of-sample result.

---

## 2. Priority matrix

| Priority | Item | Why now | Effort | Risk if deferred |
|---|---|---|---|---|
| **P0** | Fix `backtest` collision | No trustworthy baseline | S | All results suspect |
| **P0** | Persist guardian state | Restart bypasses loss limit | S | Real money loss |
| **P0** | Closed-bar ATR for risk | Wrong size/stops | S | Non-deterministic execution |
| **P0** | Quote quarantine | Invalid GBPUSD feed | S | Bad data reaches logic |
| **P0** | `backups/` in `.gitignore` | Secret leak | XS | Key committed |
| **P1** | Config schema + validation | Fail fast, safe defaults | M | Silent misconfiguration |
| **P1** | Migrations + trade tables | Learning needs data | M | No outcome analysis |
| **P1** | Test harness | Regressions | M | Every phase risky |
| **P1** | Market data engine | Consistent M1–D1 | M | Strategy bugs |
| **P1** | Five strategy engines | Core product | L | No differentiation |
| **P1** | Risk governor v2 | Required controls | L | Account damage |
| **P1** | MQL5 EA + protocol | Native execution | XL | Python dependency |
| **P1** | Research backtester v2 | Validate edge | L | False confidence |
| **P1** | FastAPI + React | Real dashboard | XL | Unusable controls |
| **P1** | ML pipeline | Probability/ranking | L | Weaker selection |
| **P1** | Committee | Explainable decisions | L | Opaque AI |
| **P1** | Learning engine | Improve over time | L | No feedback loop |
| **P2** | Telegram, exports, polish | Usability | M | Lower adoption |

S = ≤1 day, M = 2–5 days, L = 1–2 weeks, XL = 2–4 weeks for one experienced developer.
These are planning estimates, not commitments.

---

## 3. Phase plan

Each phase has: objective, deliverables, acceptance tests, and "do not break" constraints.
No phase is complete because files exist; it is complete when the acceptance tests pass.

---

### Phase 0 — Backup and baseline (complete)

**Status:** done on 2026-11-02.

- Git tag `pre-upgrade-2026-11-02`.
- `backups/2026-11-02/config.json` and `state/` copied.
- Audit documents written.
- MT5 connectivity verified.

**Remaining hygiene:** add `backups/` to `.gitignore`, verify `git status` contains no
secrets, and record the baseline commit in the README.

---

### Phase 1 — Foundation repair (complete on `upgrade/phase-1-foundation`)

**Objective:** make the existing system trustworthy and testable.

#### Deliverables

1. **Backtest consolidation**
   - Rename the package `backtest/` → `research/` (or `btengine/`), update imports in
     `main.py`, `selftest.py`, `backtest/tune.py`, `backtest/horizon_edges.py`.
   - Keep `backtest.py` as the CLI facade; have it call the fuller engine.
   - Preserve both implementations in git history; do not delete until equivalence tests
     pass.
   - Add `--engine legacy|research` only if both are genuinely needed; otherwise one engine.

2. **Guardian persistence**
   - New table/JSON: `guardian_state(day, day_start_equity, halted_day, halt_reason,
     halted_at, updated_at)`.
   - Atomic write, load on startup, day rollover logic.
   - Manual reset requires `confirm="RESET"` and writes an audit event.
   - Test: start halted, restart, still halted; next day, unlocked with new day-start equity.

3. **Closed-bar correctness**
   - `ctx.atr`/`ctx.rsi` from `closed_df` only.
   - `RiskAgent.build_plan` takes an explicit `atr` argument; no fallback to `ctx.atr`.
   - Test: extreme forming bar does not change SL/TP/volume.

4. **Quote quarantine**
   - `core/quotes.py` with `QuoteQuality` (valid, reason, age, spread).
   - All consumers use it; dashboard labels `QUARANTINED`.
   - Test matrix: zero bid, crossed, negative spread, stale, wide, valid.

5. **Config validation**
   - Pydantic v2 model or dataclass validator with ranges:
     risk 0.01–5%, max positions 1–20, spread > 0, timeframe in allowed set, paths exist,
     magic > 0, symbols non-empty, Kronos runs 1–10.
   - `config_version: 2`, defaults merge, readable error listing every problem.
   - Secret fields support env override (`TA_MT5_PASSWORD`, `TA_GEMINI_KEY`,
     `TA_TELEGRAM_TOKEN`) and are redacted in logs.

6. **Test harness**
   - `pytest`, `tests/` layout, fixtures for synthetic bars, fake MT5 client, temp DB.
   - Minimum tests: config, indicators, closed-bar discipline, risk gate matrix, sizing,
     guardian persistence, backtest determinism, executor retcodes, API contracts.
   - `python -m pytest -q` green; `python selftest.py` either fixed or replaced by pytest.

7. **Secrets hygiene**
   - `backups/` in `.gitignore`.
   - `THIRD_PARTY.md` for Kronos license/commit.
   - `python main.py check` never prints key values.

#### Acceptance

```
python main.py check                      # passes, XAUUSD valid, GBPUSD quarantined
python main.py backtest --csv demo_1min.csv --bars 500
                                          # prints deterministic report
python -m pytest -q                        # green
# restart test: guardian lockout survives
# closed-bar test: forming bar cannot change plan
git status                                 # no secrets/state/backups
```

#### Do not break

- Existing dashboard `/api/snapshot` and `/api/chart`.
- `main.py run --dry-run` and live order path.
- Kronos cache/scorecard.
- `config.json` meaning (add keys, never silently change existing ones).

---

### Phase 2 — Market data and five strategy engines (implementation complete on `upgrade/phase-2-strategies`)

**Objective:** one data engine and five genuinely independent strategies.

**Status:** the data engine, causal features, structure detectors, five engines, registry,
arbiter, replay harness and tests are implemented. The Phase 2 exit validation is
**complete and FAILED** (2026-11-12): 12,000 XAUUSD M15 bars, four expanding folds
plus an untouched 25% holdout. Out-of-sample: 327 trades, -6.87R, PF 0.95;
holdout: 102 trades, -8.52R, PF 0.83; 2 of 9 gates passed. The walk-forward,
Monte Carlo and parameter-sensitivity harness is implemented and tested
(`research/walkforward.py`, `python main.py walkforward`). No engine is promoted
and the engines remain research-only. Evidence:
[`docs/reports/phase2_walkforward_2026-11-12.md`](docs/reports/phase2_walkforward_2026-11-12.md).

#### 2.1 Market data engine

- `market/data_engine.py`:
  - `BarSeries` per symbol/timeframe with `open_time`, `close_time`, `available_at`,
    `is_closed`, OHLCV, spread.
  - Single clock; all higher-timeframe joins use `available_at` (no future HTF bar).
  - Fetch from MT5 with retry/backoff; cache in memory and SQLite/Parquet.
  - Validate monotonic timestamps, duplicates, gaps, OHLC consistency, zero/negative prices.
  - Resample from M1 when a native timeframe is unavailable; document the rule.
  - Quote quality gate and market-hours/session calendar.
- `market/features.py`: causal features only (returns, EMA structure, RSI, ROC, MACD, ATR,
  Bollinger, volume z-score, session flags, swing points, equal highs/lows, FVG, order blocks,
  premium/discount, previous-day levels).
- `market/structure.py`: deterministic swing detection, BOS/CHoCH, liquidity sweeps,
  displacement, FVG, order-block/mitigation, premium/discount.
- Tests: no-lookahead perturbation (change future bars, assert past features unchanged),
  duplicate/gap handling, session/DST boundaries.

#### 2.2 Strategy contract

```python
@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    symbol: str
    timeframe: str
    direction: int            # +1 / -1 / 0
    entry: float
    stop: float
    target: float
    rr: float
    confidence: float         # 0..1, calibrated where possible
    expires_at: float
    setup_id: str             # idempotency / dedup
    evidence: list[str]
    invalidators: list[str]
    regime: str
    session: str
```

Each engine implements `evaluate(ctx) -> list[StrategySignal]` and `should_exit(position,
ctx) -> ExitDecision`. No engine calls MT5 or the risk governor directly. Each has its own
config block and can be enabled/disabled independently.

#### 2.3 The five engines

| Engine | Core rules (deterministic, testable) | Typical exit |
|---|---|---|
| **Trend continuation** | EMA 20/50/200 stack; higher-high/higher-low or lower-high/lower-low; pullback to EMA20/50 with rejection; ATR-normalised momentum; trend strength (ADX or EMA slope); entry on continuation | Trail below swing/EMA; exit on structure break |
| **Breakout** | Asian range (00:00–07:00 UTC), London (07:00–11:00), NY (12:00–16:00); previous-day high/low; range compression (ATR percentile); breakout close beyond level + buffer; retest confirmation; volume/volatility expansion; false-break invalidation | SL beyond range; TP at 1.5–3R or next liquidity; time stop if no follow-through |
| **SMC/ICT** | Swing highs/lows; equal highs/lows; buy-side/sell-side liquidity; liquidity sweep; BOS/CHoCH; displacement (body > k×ATR); FVG (3-candle imbalance) + mitigation; order block (last opposing candle before displacement); premium/discount vs range; previous-day liquidity; killzone windows | SL beyond sweep/OB; TP at opposing liquidity/FVG; invalidate on close back through structure |
| **Momentum** | RSI(14) + ROC(10) + MACD histogram; ATR expansion; acceleration (ROC of ROC); exhaustion (RSI divergence / climactic bar); multi-timeframe agreement (M15 with H1) | Trail ATR; exit on momentum flip/exhaustion |
| **Mean reversion** | Range detection (ADX low / BB width percentile); Bollinger touch + close back inside; deviation z-score; volatility regime filter; overextension vs VWAP/EMA; reversal confirmation candle; no trades in strong trend | TP at mean/mid-band; SL beyond extreme; time stop |

**Independence requirement:** each engine must be able to produce a signal while the others
are flat, and must have its own SL/TP/R:R and performance record. They may share the data and
feature layer; they may not share a single consensus direction as their only output.

#### 2.4 Strategy registry and arbitration

- `strategies/registry.py`: register, enable/disable, parameter schema, health.
- `strategies/arbiter.py`: when multiple signals exist, rank by (validated expectancy ×
  confidence × regime fit), enforce per-strategy exposure caps, and avoid duplicate
  same-symbol/same-direction entries.
- Every signal, rejection and arbitration result is journaled with reasons.

#### 2.5 Acceptance

- Each engine has unit tests on hand-built scenarios (positive, negative, boundary).
- A 30-day replay produces per-strategy trade lists and metrics.
- No engine reads a forming bar; perturbation test passes for all five.
- Disabling one engine changes results only for that engine's trades.
- Target frequency is measured, not assumed: report signals/day per engine and combined.

---

### Phase 3 — Native MQL5 EA and protocol

**Objective:** move order placement and position management into a deterministic EA that runs
in the Strategy Tester without Python.

**Status (2026-11-11):** implementation complete and compiled cleanly
(`#property strict`, 0 errors / 0 warnings). The EA has native five-strategy
signals, broker-side stops, risk sizing, partial/BE/trail/time exits, persisted
idempotency keys, halt/resume, position snapshots, reconciliation, and
CSV/JSON tester reports. The Python loopback bridge and protocol are implemented
and covered by tests (`python main.py ea-token`, `python main.py ea-server`).

Three Strategy Tester passes on XAUUSD M15 were completed (in-sample):
202 trades over three months at -18.66R (PF 0.77), plus two one-month passes.
The results are **negative**, so the EA remains PAPER/dry-run by default and no
engine is promoted to live execution. Reports and caveats are archived in
[`docs/reports/`](docs/reports/README.md).

On 2026-11-11 the demo smoke test completed the live protocol checks: the EA
authenticated, returned a PAPER `dry_run` ack, rejected the same idempotency
key as `duplicate` after the Python cache was cleared, reconnected after a
bridge restart, and reconciled with zero mismatches. The account stayed flat.
Evidence:
[`docs/reports/phase3_ea_live_smoke_2026-11-11.md`](docs/reports/phase3_ea_live_smoke_2026-11-11.md).

Still open before this phase exits: the socket whitelist is not persisted by
terminal build 6316 (re-add `127.0.0.1` after every restart), mid-trade
kill/recovery drills, and real-tick/variable-spread/commission tester data.
The Phase 2 walk-forward/holdout gate is complete and **FAILED**; it does not
block the PAPER EA smoke work, but it blocks any live promotion.

#### 3.1 EA responsibilities

- Receive authenticated JSON intents over localhost (or a file/named-pipe fallback).
- Validate: symbol, direction, volume, SL/TP, stops level, freeze level, spread, session,
  risk caps, idempotency key.
- Place orders with broker-side SL/TP; handle retcodes and filling modes.
- Manage positions tick-by-tick: partial at R, breakeven, ATR/structure trailing, time stop,
  emergency flatten, daily lockout mirror.
- Reconcile on startup: compare EA state, Python state and broker positions/deals.
- Heartbeat both ways; if Python disappears, continue managing existing positions and block
  new entries after a grace period.
- Never trust Python blindly: re-check price, stops, volume, exposure, and duplicate keys.

#### 3.2 Protocol (documented in `docs/PROTOCOL.md`)

```
Transport: TCP 127.0.0.1:PORT (default 8765), newline-delimited JSON
Auth:      shared token (env/file), HMAC or constant-time compare, localhost bind only
Envelope:  {v, type, id, seq, ts, token, payload}
Types:     hello, heartbeat, intent, ack, reject, position_update, close_request,
           reconcile_request, reconcile_result, halt, resume
Rules:     - every intent has idempotency_key = sha256(strategy|symbol|bar_time|dir)
           - EA stores seen keys for >= 24h (file-backed)
           - ack includes broker ticket, fill price, slippage, retcode
           - timeout => UNKNOWN, never retry blindly; reconcile first
           - sequence gaps => request resync
           - clock skew > 2s => warn; > 10s => block new entries
```

#### 3.3 Strategy Tester compatibility

- EA has a `InpMode = LIVE | TESTER | PAPER` input.
- In `TESTER`, it generates signals from deterministic MQL5 rules (same parameters as the
  Python strategy, documented parity table) and never waits for Python.
- Uses only closed bars for decisions; fills at next tick/open according to the tester model.
- Supports every-tick real ticks, variable spread, commission and slippage inputs.
- Produces a machine-readable report (CSV/JSON) alongside the MT5 HTML report.

#### 3.4 Acceptance

- `#property strict`; compiles with zero errors and zero warnings in MetaEditor.
- Runs a full Strategy Tester pass on XAUUSD M15 with real ticks, variable spread and
  commission.
- Python↔EA: hello, heartbeat, PAPER intent, `dry_run` ack, duplicate
  rejection, bridge-restart reconnect and reconciliation all tested on demo
  on 2026-11-11.
- Kill Python mid-trade: EA keeps SL/TP/BE/trail and blocks new entries after grace.
- Kill EA: Python detects missing heartbeat and stops sending; positions remain protected.
- No duplicate position after a simulated timeout/retry.

---

### Phase 4 — ML and forecasting

**Objective:** add validated probability and ranking without letting an unvalidated model
trade.

#### 4.1 Feature and label pipeline

- Causal features from Phase 2 (no future data; `available_at` respected).
- Labels:
  - triple-barrier (target before stop) with configurable barriers;
  - forward return buckets;
  - regime labels from a causal classifier (HMM or rule-based trend/range/vol).
- Dataset builder with train/validation/test split by time, purge and embargo.
- Feature version hash; dataset manifest with row count, date range, symbol, timeframe.

#### 4.2 Models

| Task | Model | Output |
|---|---|---|
| Regime | Gaussian HMM or gradient boosting on causal features | trend/range/vol probability |
| Target-before-stop | XGBoost/LightGBM binary | calibrated probability |
| Expected move | quantile regression or LightGBM | p10/p50/p90 in ATR |
| Strategy ranking | LambdaRank or simple expected-R model | rank per signal |
| Failure detection | binary classifier on pre-entry features | probability of failed entry |
| RL (experimental) | PPO on a cost-aware environment | off by default |

#### 4.3 Lifecycle

- `models/registry.json`: model id, version, feature version, training window, metrics,
  status (`candidate`, `validated`, `live`, `retired`), artifact path/hash.
- Training CLI with fixed seeds and reproducible splits.
- Validation gates: AUC > 0.55 with CI, calibration (Brier), positive expectancy after costs,
  stability across folds, no leakage test.
- Promotion requires out-of-sample evidence and a risk review; rollback is one command.
- Live inference has a timeout and falls back to rules if the model is unavailable.
- Monitoring: PSI/feature drift, calibration drift, hit rate, expected vs realized R.

#### 4.4 Kronos improvements

- Preserve the current ensemble and scorecard.
- Add calibration: Brier score, MAE by horizon, directional accuracy by regime/session.
- Add horizon-specific weights, decay for stale forecasts, and a minimum sample before a
  timeframe earns weight.
- Kronos remains one input; it cannot be the sole entry authority.

#### 4.5 Acceptance

- Reproducible training run produces identical metrics on the same data/seed.
- Leakage test: perturbing future bars does not change training features/labels.
- Validation report shows out-of-sample metrics with confidence intervals.
- A deliberately shuffled-label model fails the gate and is not promoted.
- Live system works with the model disabled/unavailable.

---

### Phase 5 — AI committee

**Objective:** replace the single Gemini reviewer with a bounded, auditable committee.

#### Roles

| Role | Input | Output |
|---|---|---|
| Market analyst | structure, trend, levels, regime | directional bias + key levels |
| Bullish analyst | evidence for long | strongest long case + invalidators |
| Bearish analyst | evidence for short | strongest short case + invalidators |
| Quant analyst | strategy signals, ML probabilities, Kronos | statistical support/conflict |
| News analyst | calendar, headlines (if available) | event risk + bias |
| Risk analyst | exposure, stops, R:R, account limits | approve/reduce/veto recommendation |
| Decision agent | all of the above | BUY/SELL/WAIT/EXIT + full record |

#### Implementation rules

- Provider abstraction: Gemini, OpenAI-compatible, local (optional), mock. No mandatory paid
  API.
- Per-agent timeout (e.g. 8s), total budget (e.g. 25s), concurrency, caching by
  `(symbol, bar_time, role, context_hash)`.
- If a provider fails: mark the role unavailable, continue with remaining evidence; the
  decision agent must state missing inputs.
- The committee outputs a **proposal**, never an order. The risk governor can veto or resize
  down.
- Every decision stores: symbol, direction, strategy, entry, SL, TP, expected R:R,
  supporting evidence, conflicting evidence, invalidating conditions, per-agent opinions,
  model versions, timestamp, latency, token/cost estimate.
- Prompt injection defence: market/news text is untrusted data; never execute instructions
  found in it; strip/limit length; separate system and data channels.

#### Acceptance

- Committee runs with 0, 1 or 2 providers; no freeze when one is down.
- A forced timeout produces a logged WAIT, not a hang.
- Decision records are complete and reproducible from stored inputs.
- A red-team prompt in a news headline cannot change the output schema or trigger an order.
- Committee never calls MT5; a test asserts the execution layer is not importable from it.

---

### Phase 6 — Learning engine

**Objective:** learn from every closed trade and promote only validated changes.

#### Data model

```
trades(
  trade_id, signal_id, strategy, symbol, direction, regime, session,
  entry_ts, entry_price, exit_ts, exit_price, volume, sl, tp,
  mfe, mae, realized_r, gross_pnl, commission, swap, spread_entry, spread_exit,
  slippage_entry, slippage_exit, exit_reason, committee_decision_id,
  model_versions, news_context, duration_seconds
)
trade_events(trade_id, ts, kind, price, volume, detail)
loss_classifications(trade_id, primary, secondary, confidence, evidence)
strategy_stats(strategy, window, trades, win_rate, pf, expectancy_r, max_dd_r, sharpe)
```

#### Loss taxonomy

Wrong direction, poor entry timing, false breakout, liquidity trap, weak displacement, bad
stop placement, excessive spread, news volatility, premature exit, strategy failure,
execution failure, regime mismatch, model error.

Classification rules must be deterministic and documented; an LLM may *suggest* a label but
cannot overwrite the rule-based primary label without review.

#### Reports

- Per strategy, regime, session, month, direction, exit reason.
- MFE/MAE distributions; R-multiple histogram; expectancy with confidence interval.
- Recurring mistakes ranked by cost.
- Before/after comparison for any change, with the same validation protocol.
- Export CSV/JSON/PDF-ready HTML.

#### Promotion gate

A strategy/config/model change is promoted only if:

1. it improves out-of-sample expectancy after costs;
2. it does not increase max drawdown beyond the risk budget;
3. it is stable across at least 3 walk-forward folds;
4. it passes Monte Carlo (5th percentile expectancy > 0 or acceptable);
5. it has a rollback path and a monitoring plan.

#### Acceptance

- Every closed trade produces a complete record with MFE/MAE and realized R.
- Reconciliation against MT5 deal history matches within tolerance.
- Reports match a hand-computed fixture.
- A change that only improves in-sample results is rejected by the gate.

---

### Phase 7 — FastAPI + React dashboard

**Objective:** replace the Flask monolith with a real control surface.

#### Backend

- FastAPI, Pydantic schemas, versioned `/api/v1`.
- SQLite WAL initially; repository layer with a PostgreSQL path.
- Auth: local token (generated on first run), HttpOnly cookie or bearer; origin check.
- WebSocket `/ws` for live board, positions, equity, events, committee updates.
- Endpoints: account, positions, orders, trade history, strategies (list/enable/disable/
  params), signals, committee, market/structure, forecasts, backtests (start/status/report/
  export), risk (get/update/emergency), settings (get/update, secrets redacted), logs, health.
- Every mutating endpoint validates, audits, returns a structured result, and is idempotent.
- No endpoint returns secrets. Live-mode changes require a confirmation field.

#### Frontend

- React + TypeScript + Tailwind, dark premium terminal, responsive.
- Pages: Overview, Live Trading, AI Committee, Strategy Desk, Market Intelligence,
  Backtesting, Learning Centre, Risk Centre, Settings.
- Charts: candlesticks with markers, equity/drawdown curves, R distribution, heatmaps.
- Every button wired to a real endpoint with loading/error/success states.
- Dry-run is the default; live requires an explicit confirmation dialog and shows a persistent
  LIVE banner.
- No mock metrics: if data is unavailable, show "unavailable" with the reason.

#### Acceptance

- All pages load with real data or explicit empty/error states.
- Every control has an integration test (backend) and an E2E test (Playwright).
- Emergency close-all works in dry-run (simulated) and demo (real), with audit.
- Strategy enable/disable changes the next cycle and is reflected in the journal.
- Backtest run from the UI produces a report downloadable from the UI.
- WebSocket reconnect works after a server restart.
- Lighthouse/accessibility sanity: keyboard navigation, contrast, responsive layout.

---

### Phase 8 — Integration and verification

**Objective:** prove the system works as a whole, under failure.

#### Test matrix

| Layer | Tests |
|---|---|
| Unit | indicators, features, structure, each strategy, sizing, risk gates, guardian, config, labels |
| Integration | data→strategy→risk→intent; intent→EA→ack; journal→report; API→DB |
| E2E | dry-run day; demo trade lifecycle; restart mid-trade; disconnect/reconnect; duplicate send; bad quote; missing AI |
| Research | walk-forward, holdout, Monte Carlo, parameter stability, PBO |
| MT5 | EA compile, Strategy Tester real ticks, variable spread, commission, slippage |
| Recovery | kill Python, kill EA, kill MT5, corrupt state file, full disk (simulated) |
| Security | secret redaction, auth, injection, path traversal, oversized payloads |

#### Release checklist

- [ ] `python main.py check` passes; XAUUSD valid; invalid symbols quarantined.
- [ ] `pytest` green; coverage reported for critical modules.
- [ ] Backtest report reproducible from a manifest (data hash, config hash, code hash).
- [ ] Walk-forward and holdout results documented, including failures.
- [ ] EA compiles clean; Strategy Tester report archived.
- [ ] Demo dry-run and demo live verified; reconciliation matches.
- [ ] Dashboard controls verified; live mode gated.
- [ ] No secrets in git history or logs.
- [ ] Rollback instructions tested.
- [ ] README and subsystem docs updated.

---

## 4. Suggested sequencing (first 15 working sessions)

| Session | Work | Output |
|---|---|---|
| 1 | P0 fixes: backtest collision, guardian persistence, closed-bar ATR, quote quarantine, `.gitignore` | Phase 1 core |
| 2 | Config schema + validation + tests | Phase 1 |
| 3 | Migrations + trade/journal tables + state store refactor | Phase 1 |
| 4 | Test harness expansion; executor/risk tests | Phase 1 exit |
| 5 | Market data engine + features + leakage tests | Phase 2 |
| 6 | Trend + momentum engines + tests | Phase 2 |
| 7 | Breakout + mean reversion engines + tests | Phase 2 |
| 8 | SMC/ICT engine + structure tests | Phase 2 |
| 9 | Registry, arbiter, per-strategy journaling, replay metrics | Phase 2 exit |
| 10 | Research backtester v2: costs, walk-forward, holdout, reports | Phase 1/2 validation |
| 11 | MQL5 EA skeleton: protocol, auth, heartbeat, order path | Phase 3 |
| 12 | EA management parity: partial/BE/trail/time exit/reconcile | Phase 3 |
| 13 | Strategy Tester mode + parity report | Phase 3 exit |
| 14 | ML dataset/labels + first XGBoost model + registry | Phase 4 |
| 15 | Committee scaffold + provider abstraction + decision record | Phase 5 |

Phases 4–8 overlap in practice; the order above front-loads the dependencies. No phase is
skipped: the EA needs the strategy contract, the committee needs the risk policy, and the
dashboard needs the APIs.

---

## 5. Risk register for the upgrade

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Scope explosion (5 strategies + ML + EA + React at once) | High | Delivery stalls | Phase gates; working system at every step |
| Overfitting during strategy tuning | High | False confidence | Purge/embargo/holdout/PBO; promote only validated |
| MQL5/Python divergence | High | Different live vs test behaviour | Parity table + shared fixtures + tester mode |
| Broker restrictions (stops level, filling mode, symbol suffix) | Medium | Orders rejected | Contract-spec tests per broker; order_check first |
| Windows path/encoding issues | Medium | Startup failures | Pathlib, UTF-8, CI on Windows |
| Data gaps/weekends/DST | Medium | Bad features/sessions | Single clock, session calendar, gap tests |
| AI provider outage/cost | Medium | Committee unavailable | Timeouts, caching, provider abstraction, rules fallback |
| Secret leakage | Medium | Account/API compromise | Env/keyring, redaction, git checks |
| Duplicate orders | Medium | Double risk | Idempotency keys + broker reconciliation |
| Dashboard/API abuse | Low | Unauthorized trading | Local token, live confirmation, audit |
| User impatience / skipped phases | Medium | Unsafe system | Plain-language status; show evidence, not claims |

---

## 6. Definition of done (mapped to the brief)

| Brief requirement | Phase | Evidence |
|---|---|---|
| Audit and preserve existing features | 0–1 | Audit docs, tag, tests |
| MT5 connects reliably | 1 | `main.py check`, reconnect tests |
| Real XAUUSD data | 2 | Data engine + quality tests |
| Five independent strategies | 2 | Five engines, separate P/L |
| Strategy decisions logged | 1–2 | `signals`/`decisions` tables |
| Risk controls enforced | 1, 3 | Policy tests + EA mirror |
| Native MQL5 EA compiles | 3 | MetaEditor build log |
| EA runs in Strategy Tester | 3 | Tester report |
| Python↔MQL5 reliable | 3 | Protocol tests, recovery tests |
| AI agents graceful failure | 5 | Timeout/outage tests |
| ML reproducible | 4 | Manifest, seeds, metrics |
| Trades reconciled | 3, 6 | Deal-history reconciliation |
| Dashboard real data | 7 | API/E2E tests |
| Every control works | 7 | Control matrix + E2E |
| Trade outcomes analyzed | 6 | Trade store + reports |
| Demo verified | 8 | Demo run log |
| No live without activation | 1, 7 | Default dry-run + confirmation |

---

## 7. Immediate next actions (Phase 1)

1. Add `backups/` to `.gitignore`; verify `git status` has no secrets.
2. Rename `backtest/` package to `research/`; update imports; make
   `python main.py backtest --csv demo_1min.csv` work.
3. Persist guardian state; add restart test.
4. Use closed-bar ATR in `RiskAgent.build_plan`; add forming-bar test.
5. Add `QuoteQuality` and display quarantine on the dashboard.
6. Add config schema validation with readable errors.
7. Add the first pytest suite (config, indicators, risk gates, guardian, backtest).
8. Commit and push the Phase 1 branch after verification.

Only after these pass should Phase 2 begin.
