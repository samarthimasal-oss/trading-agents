# Repository Evaluation — Reference Study

**Date:** 2026-11-02
**Scope:** four GitHub projects named in the brief, reviewed at source level (metadata, README/
SPEC, recursive tree, selected modules), compared against the existing `trading_agents`
codebase.
**Rule:** no repository is cloned, merged or copied wholesale. Ideas are reimplemented only
where compatible. Licenses are respected; AGPL code is read for ideas but not copied.

---

## 0. Summary table

| Project | License | Scale (files / approx. LOC) | Primary focus | Fit to this brief | Decision |
|---|---|---|---|---|---|
| [Aurum / `zero-was-here/tradingbot`](https://github.com/zero-was-here/tradingbot) | **MIT** | ~250 files / ~35k LOC | XAUUSD systematic research, point-in-time data, 15 strategies, walk-forward, risk, LLM desk | **High** for research/risk/architecture | **Adapt concepts; no wholesale copy** |
| [`MooreSi/forex-gold`](https://github.com/MooreSi/forex-gold) | **AGPL-3.0** | ~2,343 files | XAUUSD MT5 execution, FastAPI + React dashboard, risk governor, EA bridge, Telegram signals | **High** for EA/dashboard/risk patterns | **Ideas only; no code copy** |
| [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents) | **Apache-2.0** | ~213 files | Multi-agent equity research/debate with LangGraph | **Medium** for committee roles | **Adapt role/debate structure; not the equity data stack** |
| [`AI4Finance-Foundation/FinRL-Trading`](https://github.com/AI4Finance-Foundation/FinRL-Trading) | **Apache-2.0** | ~84 files | Stock rotation, ML/DRL, `bt`-based portfolio backtests | **Low–medium** for ML research hygiene | **Selective: walk-forward/validation ideas only** |

**Stars are not evidence of edge.** Aurum has 214 stars and documents *no demonstrated edge*
on 14 years of XAUUSD. TradingAgents has ~119.8k stars but is an equity research workflow,
not an execution system. forex-gold has 8 stars but contains the most relevant MT5/EA bridge
architecture. FinRL-X has ~3.9k stars and is stock-focused. Select by fit, not popularity.

---

## 1. Aurum — `zero-was-here/tradingbot` (MIT)

### 1.1 What it is

A rebuild of an earlier "AI gold bot" into an institutional-style systematic research stack
for spot gold. It covers point-in-time data (Dukascopy bid/ask, Yahoo/FRED macro), a causal
feature library, 15 strategies (rule-based, ML, RL), net-of-cost portfolio construction,
volatility targeting, a hard risk manager, an event-driven simulator with spread/slippage/
commission/financing, walk-forward research with deflated statistics, and an LLM trading desk
(Claude CIO + specialists) that cannot bypass risk. It also ships a paper broker and an MT5
adapter.

### 1.2 Architecture observed

```
data (PIT, available_at)
  → features (12 causal groups, leakage-tested)
  → strategies (forecast in [-1, +1], causal at bar close)
  → combiner (net-of-cost Sharpe shrinkage / inverse-vol / HRP)
  → sizer (vol targeting, drawdown de-risking)
  → risk manager (reduce-only, kill switch persisted)
  → simulator / paper / MT5 OMS
```

Key source-level facts:

- `aurum/strategies/base.py`: strategies return a **forecast**, not an order. The contract
  explicitly requires causality (`forecast[t]` depends only on data available at close of
  `t`), warm-up zeros, no NaN/inf, and `fit` receives only training data.
- `aurum/backtest/engine.py`: backtest and live share the same sizer, risk manager and
  execution simulator. Orders fill at the **open of t+1**; stops are anchored at the fill;
  risk interventions are recorded; the engine enforces reduce-only clamping.
- `aurum/research/walkforward.py`: rolling folds, **purge + embargo** (at least the largest
  label horizon), per-fold scaler fitting, stitched out-of-sample forecasts, **one**
  continuous OOS backtest, daily Sharpe, PSR, **Deflated Sharpe**, PBO (CSCV), bootstrap CI,
  holdout ledger, cost attribution.
- `aurum/risk/manager.py`: kill switch persisted to disk, daily loss vs day-start equity,
  exposure/leverage/margin caps, event blackouts, spread/stale-data/trade-count guards,
  reduce-only invariant, rounding toward zero.
- `aurum/execution/costs.py`: spread crossing, slippage (fixed + range fraction + sqrt impact),
  commission, rate-based financing with triple-Wednesday and point-in-time Fed funds.
- `aurum/features/pit.py` (path present; content not needed): point-in-time joins with
  `available_at`.
- `mt5_ea/GoldHedgerPro_v4.mq5`: exists, but it is a **grid/hedge EA with lot multiplier
  1.5** — exactly the pattern this project forbids. Its presence is not an endorsement.

### 1.3 Research result (critical context)

`docs/RESULTS.md` reports a pre-registered walk-forward on 2013–2024 XAUUSD H1:

- Combined book Sharpe **−0.26** (95% CI [−0.95, 0.38]), DSR 0.003.
- Mean reversion and opening-range breakout strategies **lose reliably after costs**
  (z-score fade −1.51, RSI2 −1.06, Bollinger −1.52, ORB −1.58 Sharpe).
- `ml_gbm` failed its validation skill gate in every fold (AUC 0.43–0.57) and made no trades.
- Best walk-forward DSR 0.05 against a 0.95 bar; PBO 0.41; probability the in-sample best
  loses OOS 0.81.
- The 2025–26 holdout looked good (combined Sharpe 1.34) but the CI includes zero; the authors
  explicitly say **no strategy or book has a demonstrated edge** and recommend research/paper
  only.

**Implication for this project:** the brief's target of 2–3 opportunities/day is a frequency
goal, not proof of profitability. Intraday mean reversion and naive ORB are especially risky
after costs on gold. The upgrade must measure per-strategy expectancy honestly and be willing
to disable strategies that do not pass validation. This is the single most valuable lesson
from the reference set.

### 1.4 What to reuse / adapt / reject

| Area | Decision | How |
|---|---|---|
| Point-in-time discipline (`available_at`) | **Adopt concept** | Add `available_at` to every bar/feature; HTF joins only when published |
| Strategy contract (forecast, causal, fit on train only) | **Adapt** | Use a `StrategySignal` contract with explicit causality tests |
| Leakage tests (future perturbation) | **Adopt** | Unit test: mutate future bars, assert past features unchanged |
| Walk-forward with purge/embargo, stitched OOS, one final backtest | **Adopt** | Phase 1/2 research engine |
| Deflated Sharpe, PBO, bootstrap CI, holdout ledger | **Adopt** | Report alongside standard metrics; do not overclaim |
| Cost model (spread/slippage/commission/financing) | **Adapt** | Simpler XAUUSD model with broker specs; keep the components |
| Reduce-only risk manager + persisted kill switch | **Adopt** | Phase 1 governor; mirror in MQL5 |
| "Same sizer/risk/simulator in research and live" | **Adopt** | Single policy module; parity tests |
| LLM desk bounded by risk | **Adapt** | Committee proposes; governor disposes |
| 15-strategy catalogue | **Reject as-is** | Too broad; build the five required engines first |
| RL as a live component | **Reject for now** | Experimental only, off by default |
| GoldHedgerPro grid/hedge EA | **Reject** | Violates no-martingale/no-grid rule |
| Full 35k-LOC framework | **Reject** | Too heavy for a local Windows app; borrow patterns, not mass |

### 1.5 License/attribution

MIT permits reuse with copyright/license notice. Even so, the recommendation is to
**reimplement** the patterns in this codebase's style and credit Aurum in `THIRD_PARTY.md`
and the relevant module docstrings. Do not copy the 35k-LOC library.

---

## 2. forex-gold — `MooreSi/forex-gold` (AGPL-3.0)

### 2.1 What it is

A large, actively engineered XAUUSD trading application: Python backend, React 19 + Tailwind 4
frontend, FastAPI ASGI server, SQLite/Postgres adapters, MT5 integration, a native MQL5 EA
bridge, Telegram signal ingestion, multiple strategy engines (breakout, reversal, trend
price-action, ORB), a risk governor, analytics, backtesting, Monte Carlo, and a commercial
licence/activation layer.

Observed scale: **2,343 files**, 462 frontend files, extensive tests, migrations, docs under
`docs/system/domains/*`.

### 2.2 Architecture observed

- `backend/src/api/server.py`: FastAPI composition root; routers registered before the SPA
  fallback (a deliberate, documented ordering); session middleware; static bundle served by
  Python.
- `frontend/package.json`: React 19, Vite 6, Tailwind 4, Radix UI, `lightweight-charts`,
  Vitest, Testing Library. The compiled `dist/` is committed and served by Python, so Node is
  build-time only.
- `backend/src/services/broker/ea_bridge/`: **newline-delimited JSON over localhost**, one
  connection, EA connects out to Python; heartbeat health; Python fallback watchdog takes
  over management if the EA disconnects; version handshake; comment-based trade IDs.
- `backend/src/services/broker/dedup.py`: three-state dedup — **found / absent / unknown** —
  with the explicit rule that "a broker that could not be asked has not said no"; only
  `safe_to_send` when not found and not unknown; distinct `ea:` vs `py:` comment prefixes;
  31-char MT5 comment limit handled.
- `backend/src/services/broker/position_sync.py`: reconcile vanished tickets with a **miss
  streak threshold**, record closes, sync profit, import externally opened positions; shared
  mutable streak state (documented as load-bearing).
- `backend/src/services/risk/governor.py`: deterministic app-wide safety layer; risk-%
  sizing, per-trade $ ceiling, daily-loss halt, loss-streak cooldown, R:R floor, directional
  and stop-width caps; **never places or modifies orders**; failure direction is
  refuse-to-trade.
- `backend/src/services/risk/lot_sizing.py`: one place decides risk-% vs fixed lots; refuses
  "unplaceable" sizes below broker minimum; documents a real incident where two paths
  disagreed about 0 lots and sent invalid volume.
- `docs/system/domains/risk/README.md`: gate ordering, manual-order exemption, schedule gate
  only on the automated path, circuit breaker persisted.
- `backend/src/config/licence/*`: commercial licence activation/fingerprint/guard. Not
  relevant to this project and a reason **not** to copy code.

### 2.3 Strengths

1. The EA bridge is the closest match to the brief's Python↔MQL5 requirement.
2. Dedup's three-state model is exactly the right safety design.
3. Position reconciliation with a miss streak avoids treating a transient broker hiccup as a
   close.
4. Risk governor is deterministic, gate-only, fail-closed for new risk, and has loss-streak
   cooldown.
5. React/FastAPI structure and charting choices are a useful benchmark.
6. Heavy test culture, migrations, domain docs.

### 2.4 Weaknesses / risks for this project

1. **AGPL-3.0**: copying code would impose AGPL on this project. Read for ideas only; write
   original implementations.
2. Enormous surface area (2.3k files, cluster/remote/licence/Telegram ingestion) — far beyond
   a local single-user system.
3. Commercial licence guard and activation server are unrelated and undesirable here.
4. Telegram signal ingestion (copy-trading channels) is a different product model; this
   project generates its own signals.
5. Some risk code is intertwined with app config/DB and incident-specific exceptions; not a
   clean library to lift.
6. Not proof of profitability; no out-of-sample track record is presented in the README.

### 2.5 What to reuse / adapt / reject

| Area | Decision | How |
|---|---|---|
| Localhost NDJSON EA bridge, EA connects to Python | **Adopt concept** | Phase 3 protocol; add auth token, seq, idempotency |
| Heartbeat + Python fallback watchdog | **Adopt concept** | EA manages positions; Python detects missing heartbeat |
| Three-state dedup (found/absent/unknown) | **Adopt concept** | Implement independently with tests |
| Position reconciliation with miss streak | **Adopt concept** | Require N consecutive misses before recording a close |
| Risk governor as gate-only, refuse-to-trade on failure | **Adopt concept** | Phase 1/3 policy module |
| One lot-sizing decision point; refuse unplaceable size | **Adopt concept** | Test risk-% vs fixed vs broker min |
| FastAPI + React + lightweight-charts + Tailwind | **Adopt stack choice** | Phase 7, original code |
| Domain docs + migrations + test culture | **Adopt practice** | All phases |
| AGPL source code | **Reject copying** | Ideas only; attribution note |
| Cluster/remote/licence/Telegram ingestion | **Reject** | Out of scope |
| Enormous config surface | **Reject** | Keep config small and validated |

### 2.6 License/attribution

AGPL-3.0 is copyleft and network-use triggering. **No code may be copied into this project**
unless the entire project is AGPL and the user accepts that. The safe path: independent
implementation of the architectural ideas, with a written attribution/reading note in
`THIRD_PARTY.md`. If any code fragment is ever adapted, legal review is required first.

---

## 3. TradingAgents — `TauricResearch/TradingAgents` (Apache-2.0)

### 3.1 What it is

A multi-agent LLM framework for equity research. Analyst agents (market, fundamentals, news,
sentiment) produce reports; bull and bear researchers debate; a research manager decides; a
trader proposes; aggressive/conservative/neutral risk analysts debate; a portfolio manager
finalises. Built on LangGraph with checkpointing, memory/reflection, provider abstraction and
a large test suite. A backtest harness scores decision quality over a grid of ticker/date
cells; it explicitly does **not** simulate a portfolio.

### 3.2 Architecture observed

- `tradingagents/graph/setup.py`: analyst subgraphs with private message histories, bounded
  tool rounds, wrap-up prompts to prevent runaway recursion, explicit path maps for every
  router target, `DEBATE_PATH_MAP` and `RISK_ANALYSIS_PATH_MAP`.
- `tradingagents/agents/researchers/bull_researcher.py`, `bear_researcher.py`: opposing
  arguments over shared reports.
- `tradingagents/agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py`: risk debate
  with explicit prompts to counter the other sides; portfolio context injected.
- `tradingagents/agents/managers/research_manager.py`, `portfolio_manager.py`: synthesis.
- `tradingagents/memory/`: decision log, reflection, settlement; outcomes settled later.
- `tradingagents/backtest.py`: grid of (ticker, date) cells; scores ratings vs realized/alpha;
  **not** a portfolio simulator, by design.
- Provider layer supports OpenAI, Anthropic, Google, Azure, Bedrock, OpenRouter, Ollama,
  OpenAI-compatible endpoints; API-key handling and model catalog.
- Look-ahead tests for fundamentals/news/social data; prompt-integrity tests.

### 3.3 Strengths

1. Clear separation of specialist roles, debate, management and final decision.
2. Bounded tool rounds and explicit router maps — production-minded graph design.
3. Memory/reflection/settlement loop is a good model for the learning engine.
4. Provider abstraction and graceful provider handling.
5. Strong test culture, including look-ahead and prompt-integrity tests.
6. Apache-2.0 is permissive and compatible with reuse/adaptation.

### 3.4 Weaknesses / risks for this project

1. **Equity research, not intraday XAUUSD execution.** Fundamentals/sentiment data and
   ticker/date cells do not map to M1–D1 gold trading.
2. LLM calls are central; latency/cost make it unsuitable as a per-bar gate without
   timeouts/caching.
3. LangGraph adds a heavy dependency and a graph runtime that is unnecessary for a local
   single-user system; a small async orchestrator is easier to test.
4. No portfolio/execution model; its backtest explicitly cannot be used for performance.
5. Prompt-based agents can be manipulated by untrusted news text; needs hardening.
6. No risk governor with veto power over orders; risk debate is advisory.

### 3.5 What to reuse / adapt / reject

| Area | Decision | How |
|---|---|---|
| Specialist roles (market/bull/bear/quant/news/risk/decision) | **Adopt** | Phase 5 role set, simplified |
| Opposing bull/bear debate with evidence | **Adopt** | Structured JSON arguments, not free-form only |
| Risk debate (aggressive/conservative/neutral) | **Adapt** | One risk analyst + governor; avoid three LLM calls per bar |
| Bounded tool rounds / wrap-up | **Adopt concept** | Per-agent timeout and token budget |
| Memory/reflection/settlement | **Adapt** | Phase 6 trade learning, but rule-based labels first |
| Provider abstraction | **Adopt** | Gemini/OpenAI-compatible/mock, no mandatory paid API |
| Look-ahead and prompt-integrity tests | **Adopt practice** | Phase 4/5 tests |
| LangGraph runtime | **Reject** | Use a small async orchestrator |
| Equity dataflows (Yahoo/SEC/Reddit/StockTwits) | **Reject** | Not relevant to XAUUSD intraday |
| Decision-quality grid backtest | **Reject as performance proof** | Use the research backtester + MT5 tester |

### 3.6 License/attribution

Apache-2.0 permits reuse with notices and patent grant. If code is adapted, retain the
license/NOTICE and document the files. Preferred: reimplement the role/debate pattern with
original prompts and orchestration, credit TradingAgents in `THIRD_PARTY.md`.

---

## 4. FinRL-X — `AI4Finance-Foundation/FinRL-Trading` (Apache-2.0)

### 4.1 What it is

A stock-portfolio research framework: S&P 500 universe management, fundamental/price data,
adaptive rotation strategies, ML bucket selection, DRL, `bt`-based portfolio backtests,
walk-forward analysis, a Streamlit dashboard.

### 4.2 Architecture observed

- `src/strategies/adaptive_rotation/`: market regime, group strength, intra-group ranking,
  portfolio builder, risk manager, walk-forward.
- `src/strategies/adaptive_rotation/walk_forward.py`: explicit point-in-time slicing,
  train/test windows, decision dates, config hash, valid-period handling.
- `src/backtest/backtest_engine.py`: `bt` library, portfolio returns, weights history,
  benchmark comparison, transaction cost, integer positions caveat.
- `src/strategies/ml_strategy.py`, `ml_bucket_selection.py`, `rl_model.py`: ML/DRL components.
- `src/trading/alpaca_manager.py`: US equities broker.
- `src/web/`: Streamlit app.

### 4.3 Strengths

1. Clean walk-forward abstractions with point-in-time guarantees and config hashing.
2. Regime + group strength + ranking is a useful pattern for strategy arbitration.
3. Cost-aware portfolio backtesting with benchmark comparison.
4. Apache-2.0.

### 4.4 Weaknesses / risks

1. **Stock rotation, not XAUUSD intraday.** Universe selection, fundamentals and Alpaca
   execution do not transfer.
2. `bt` assumes portfolio weights and integer shares; FX/metals lot sizing and MT5 contract
   specs need a different simulator.
3. DRL results are hard to reproduce and easy to overfit; not a live authority.
4. Streamlit dashboard is not the target stack.
5. No MT5/MQL5 execution.

### 4.5 What to reuse / adapt / reject

| Area | Decision | How |
|---|---|---|
| Walk-forward period objects, config hash, point-in-time slicing | **Adopt concept** | Research engine |
| Regime → group strength → ranking | **Adapt** | Strategy arbiter (regime fit × expectancy) |
| Cost-aware benchmark comparison | **Adopt concept** | Reports include buy-and-hold XAUUSD |
| `bt` library / stock universe / Alpaca | **Reject** | Wrong instrument and execution model |
| DRL as live strategy | **Reject for now** | Experimental, off by default |
| Streamlit | **Reject** | React/FastAPI target |

### 4.6 License/attribution

Apache-2.0; same notice obligations as TradingAgents. Likely no code reuse; credit ideas in
`THIRD_PARTY.md` if any walk-forward structure is adapted.

---

## 5. Cross-repository comparison

| Capability | Aurum | forex-gold | TradingAgents | FinRL-X | This project today | Target |
|---|---|---|---|---|---|---|
| XAUUSD focus | ✅ | ✅ | ❌ equities | ❌ stocks | ✅ (not primary) | ✅ primary |
| Point-in-time data | ✅ strong | partial | partial (equity) | ✅ walk-forward | ❌ | ✅ |
| Causal/leakage tests | ✅ | partial | ✅ | partial | ❌ | ✅ |
| Five independent strategies | ✅ 15 | ✅ several | ❌ | ✅ rotation | ❌ voters | ✅ five |
| SMC/ICT structure | partial | ✅ trend/PA | ❌ | ❌ | ❌ | ✅ |
| ML pipeline | ✅ (failed gate) | ✅ | ❌ | ✅ | ❌ | ✅ |
| Kronos-style forecast | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ improve |
| Multi-agent committee | ✅ LLM desk | partial AI | ✅ strong | ❌ | ❌ one reviewer | ✅ seven roles |
| Independent risk governor | ✅ | ✅ strong | advisory | partial | partial | ✅ |
| Persisted kill switch | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| Native MQL5 EA | ⚠️ grid EA (reject) | ✅ bridge EA | ❌ | ❌ | ❌ | ✅ original |
| EA Strategy Tester mode | ? | ✅ | ❌ | ❌ | ❌ | ✅ |
| Dedup/reconciliation | partial | ✅ strong | ❌ | ❌ | ❌ | ✅ |
| FastAPI + React dashboard | ❌ | ✅ | ❌ | Streamlit | Flask monolith | ✅ |
| Tests | ✅ ~1,450 | ✅ extensive | ✅ extensive | partial | ❌ | ✅ |
| Honest performance reporting | ✅ exemplary | unclear | N/A (research) | partial | ❌ | ✅ |
| License compatibility | MIT ✅ | AGPL ⚠️ | Apache ✅ | Apache ✅ | — | — |

---

## 6. What we will build, and from where

| Target component | Primary inspiration | Implementation stance |
|---|---|---|
| Market data engine with `available_at` | Aurum | Original Python; MT5 source, not Dukascopy |
| Feature/structure library | Aurum + forex-gold | Original; SMC rules written from first principles |
| Five strategy engines | Brief + Aurum strategy contract + forex-gold breakout/trend ideas | Original code, deterministic rules, separate P&L |
| Strategy arbiter | FinRL-X regime/ranking + Aurum combiner | Simple expected-R ranking, not HRP |
| Research backtester | Aurum walk-forward + FinRL-X period objects | One engine, costs, purge/embargo, holdout |
| ML pipeline | Aurum feature/label discipline + FinRL-X ML structure | XGBoost/LightGBM, registry, validation gate |
| Kronos | Existing project | Preserve, add calibration/error metrics |
| Committee | TradingAgents roles/debate + Aurum bounded desk | Original async orchestrator, JSON schemas, timeouts |
| Risk governor | Aurum reduce-only + forex-gold gate-only/loss-streak | Original policy module + MQL5 mirror |
| MQL5 EA/protocol | forex-gold ea_bridge/dedup/reconcile concepts | Original MQL5 + Python; auth, seq, idempotency |
| Trade learning | TradingAgents memory/settlement + brief taxonomy | Rule-based classifier first, LLM suggestions second |
| Dashboard | forex-gold stack choices + existing glass design | Original React/FastAPI app; all controls wired |
| Testing | Aurum/forex-gold/TradingAgents practices | pytest + Playwright + MT5 tester |

---

## 7. What we explicitly will **not** do

1. **No wholesale clone/merge.** The projects are references, not dependencies.
2. **No AGPL code copy** from forex-gold.
3. **No grid, hedge basket, martingale or lot multiplier** — including Aurum's
   `GoldHedgerPro_v4.mq5`, despite its MIT license.
4. **No equity/fundamentals data stack** from TradingAgents.
5. **No stock rotation/DRL live strategy** from FinRL-X.
6. **No RL or ML in the live path without out-of-sample validation** and a rollback.
7. **No claims of profitability** based on stars, READMEs or in-sample results.
8. **No added framework complexity** (LangGraph, Streamlit, `bt`) unless it earns its place.

---

## 8. License and attribution plan

Create `THIRD_PARTY.md` with:

| Project | License | Use | Obligation |
|---|---|---|---|
| Aurum | MIT | Architectural concepts, research protocol ideas | Attribution + MIT notice if any code is adapted |
| forex-gold | AGPL-3.0 | Architectural concepts only; **no code** | No notice required for ideas; document the reading; legal review if code ever adapted |
| TradingAgents | Apache-2.0 | Role/debate concepts; possible prompt structure adaptation | Apache notice/NOTICE if code adapted |
| FinRL-X | Apache-2.0 | Walk-forward/validation concepts | Apache notice if code adapted |
| Kronos | check upstream license | Vendored model code | Record commit hash + license; keep notices |
| MetaTrader5 | proprietary free package | Broker API | Follow MetaQuotes terms |

Also record the exact commit hash reviewed for each reference (reproducibility). Do not
include reference repositories as git submodules; they are not dependencies.

---

## 9. Bottom line

- **Aurum** is the best source of *research discipline*: point-in-time data, leakage tests,
  walk-forward with purge/embargo, deflated statistics, reduce-only risk, and an honest
  no-edge verdict. Copy the discipline, not the 35k LOC.
- **forex-gold** is the best source of *execution and dashboard architecture*: EA bridge,
  three-state dedup, reconciliation, gate-only risk governor, FastAPI + React. Its AGPL
  license means ideas only.
- **TradingAgents** is the best source of *committee role structure* and bounded agent
  orchestration, but its equity workflow, LangGraph runtime and advisory risk model do not
  fit directly.
- **FinRL-X** contributes walk-forward/validation structure and regime-ranking ideas; its
  stock universe, `bt` simulator and DRL are not a fit.

The upgrade should be **original code with explicit provenance**, not a collage. The most
important transferable lesson is Aurum's: measure honestly, expect some strategies to fail,
and do not let a sophisticated-looking stack substitute for out-of-sample evidence.
