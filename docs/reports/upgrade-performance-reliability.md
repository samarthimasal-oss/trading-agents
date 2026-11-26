# Performance & reliability upgrade — measurement report

**Date:** 2026-11-26  
**Branch:** `upgrade/performance-reliability`  
**Scope:** XAUUSD-first signal reliability, broker-accurate sizing/execution,
real-data funnel analytics, and a causal research backtest. No live promotion.

## 1. What changed

| Phase | Commit | Change |
|---|---|---|
| 1 | `b444e86` | Signal/backtest parity: 300-bar causal window, forming-bar drop, closed-bar indicators/ATR, advisory Gemini semantics (`require_llm_approval=false`), per-symbol LLM rate limit, Kronos ensemble runs, cooldown recorded |
| 2 | `007d947` | Risk/execution: broker-calculated sizing from the actual entry/SL, round **down** to the broker step, reject a minimum lot that would exceed the risk budget, margin/leverage cap, deal-confirmed fills, duplicate/margin/symbol/SL-TP guards, send-error reconciliation, reliable SL modification, fail-closed guardian |
| 3 | `a88a920` | Real-data funnel: `evaluations` journal (one row per stage per symbol per closed bar), `GET /funnel`, Signal Funnel dashboard with rejection reasons and bot-only closed trades |
| 4 | this branch tip | Research backtest realism: next-bar fills, bid/ask spread, gap-through fills, SL-before-TP, round-turn commission, exit slippage, spread floor, no pre-entry stop management, M5/M15 caches and cooldown scaling, extended metrics, before/after measurement |

The live runner still uses the five quant agents + Kronos + Gemini advisory
review + risk guardian. No mandatory filter was added, so the signal path is
the same one the backtest replays.

## 2. How the measurement was produced

Command:

```powershell
python -m research.measure_upgrade --baseline-ref a88a920
```

The tool replays the **same cached bars and agent votes** through two engines:

* **Baseline** — `research/backtest.py` at commit `a88a920`, the engine that
  produced earlier reports. It used `risk._symbol`, a 5-bar M15 cooldown, and
  checked exits against the bar high/low only. It also managed a position
  filled at the next open using the **pre-entry** bar's close.
* **New** — this tree's engine, with the symbol and timeframe passed
  explicitly, gap fills, costs, and no management before the entry bar closes.

Cached M15 data (broker bars, `state/bt_*.csv`; signals in
`state/bt_sig_*.pkl`):

| Symbol | Bars | First bar | Last bar |
|---|---:|---|---|
| XAUUSD | 7,980 | 2026-06-22 03:15 | 2026-10-26 21:15 |
| EURUSD | 7,980 | 2026-07-01 18:30 | 2026-10-26 21:15 |
| GBPUSD | 7,980 | 2026-07-01 18:30 | 2026-10-26 21:15 |
| USDJPY | 7,980 | 2026-07-01 18:30 | 2026-10-26 21:15 |

Common replay settings: 300-bar warmup, 0.5% risk per trade, no fixed lot,
2.5× notional leverage cap, max 1 position per symbol. The four symbols were
replayed independently on their own 100,000 USD account; the aggregate is a
sum, **not** a portfolio simulation (live concurrency is capped separately).

### Cost assumptions (labelled, not realised fills)

| Cost | Value | Source |
|---|---:|---|
| Commission | $7.00 per lot round turn | assumption (typical raw-account level) |
| Slippage | 2 points per fill | assumption |
| Spread floor XAUUSD | 20 points ($0.20) | assumption |
| Spread floor EURUSD | 8 points (0.8 pip) | assumption |
| Spread floor GBPUSD | 10 points (1.0 pip) | assumption |
| Spread floor USDJPY | 8 points (0.8 pip) | assumption |

The CSV spread is already broker data, but it is mostly 0 on this demo feed
(mean XAUUSD 8.36 points; FX ≈ 0.01–0.22). The replay charges
`max(recorded, floor)` on every bar, so the cost run is deliberately
conservative. The floor is a **minimum**, not a claim that the real spread is
constant.

### Execution model

* a decision on the close of bar *i* fills at the open of bar *i+1*;
* bars are BID quotes; a long buys the ask (bid + spread), a short exits at the
  ask;
* a bar that opens beyond a stop/target fills at the gap open, not the level;
* the stop is assumed before the target when one bar touches both;
* commission is charged once per round turn; slippage is adverse on every fill;
* a new position is not managed until its entry bar has closed, so a
  pre-entry close can never move its stop;
* the cooldown is `max(1, ceil(seconds / bar_seconds))`, so it is never shorter
  than the live 300-second gap (1 bar on M15, 3 bars on M5).

## 3. Results (M15, 2026-06-22 → 2026-10-26)

`new zero (same cooldown)` forces the old 5-bar cooldown. `new zero` uses the
corrected 1-bar cooldown. `new + costs` adds the spread floor, commission and
slippage above. The same-cooldown comparison is **not** a pure fill-isolation
run: the new engine also stops managing next-bar fills with the pre-entry
close, which changes trade sequencing (see section 4).

| Symbol | Variant | Trades | Net P/L USD | Win rate | PF | Max DD USD |
|---|---|---:|---:|---:|---:|---:|
| XAUUSD | baseline | 1,116 | **+14,116.92** | 36.6% | 1.05 | 12,369.07 |
| XAUUSD | new zero (same cooldown) | 1,079 | **+4,990.26** | 39.5% | 1.02 | 21,305.55 |
| XAUUSD | new zero | 1,325 | -5,439.94 | 37.9% | 0.99 | 29,393.07 |
| XAUUSD | new + costs | 1,325 | **-23,769.14** | 36.8% | 0.93 | 33,970.40 |
| EURUSD | baseline | 1,066 | -7,380.45 | 34.8% | 0.87 | 9,423.56 |
| EURUSD | new zero (same cooldown) | 1,020 | -5,892.89 | 38.3% | 0.90 | 7,963.49 |
| EURUSD | new zero | 1,322 | -8,340.88 | 37.6% | 0.89 | 9,057.51 |
| EURUSD | new + costs | 1,370 | **-54,207.33** | 25.0% | 0.39 | 54,224.80 |
| GBPUSD | baseline | 1,076 | -12,446.84 | 31.8% | 0.79 | 13,615.29 |
| GBPUSD | new zero (same cooldown) | 1,035 | -13,182.14 | 34.8% | 0.79 | 15,400.93 |
| GBPUSD | new zero | 1,319 | -14,007.39 | 34.7% | 0.82 | 14,902.75 |
| GBPUSD | new + costs | 1,347 | **-54,337.10** | 25.7% | 0.40 | 54,337.10 |
| USDJPY | baseline | 1,081 | -6,700.40 | 33.6% | 0.91 | 7,244.92 |
| USDJPY | new zero (same cooldown) | 1,049 | -10,431.68 | 36.0% | 0.87 | 11,291.10 |
| USDJPY | new zero | 1,327 | -11,731.13 | 35.7% | 0.89 | 13,809.53 |
| USDJPY | new + costs | 1,341 | **-49,240.07** | 28.3% | 0.55 | 49,327.80 |
| **Aggregate** | baseline | 4,339 | **-12,410.77** | 34.2% | 0.97 | — |
| **Aggregate** | new zero (same cooldown) | 4,183 | **-24,516.45** | 37.2% | 0.95 | — |
| **Aggregate** | new zero | 5,293 | -39,519.34 | 36.5% | 0.94 | — |
| **Aggregate** | new + costs | 5,383 | **-181,553.64** | 28.9% | 0.72 | — |

Aggregate per-trade expectancy: baseline −$2.86, new zero (same cooldown)
−$5.86, new zero −$7.47, new + costs −$33.73.

XAUUSD is the only positive cell in the zero-cost rows, and its sign is not
robust: it is +$4,990.26 at the forced 5-bar cooldown and −$5,439.94 at the
corrected 1-bar cooldown. After costs it is −$23,769.14 (PF 0.93).

## 4. The old XAUUSD profit was inflated by an execution artifact

The baseline checked only `low <= SL` (long) or `high + spread >= SL` (short).
When a bar **opened through** the stop — common after a fast move or a trailing
stop that ended the previous bar just above entry — it still filled at the stop
level. That fill was not available in the market.

`research/measure_upgrade.py` recomputes each baseline exit against the bar
open:

| Symbol | Stops filled better than the open | Stop overstatement | Targets filled worse than the open | Target understatement | Net bias |
|---|---:|---:|---:|---:|---:|
| XAUUSD | 24 | +$19,552.20 | 2 | -$1,422.36 | **+$18,129.84** |
| EURUSD | 7 | +$543.81 | 1 | -$418.14 | +$125.67 |
| GBPUSD | 9 | +$1,652.75 | 0 | $0.00 | +$1,652.75 |
| USDJPY | 10 | +$1,554.42 | 0 | $0.00 | +$1,554.42 |
| **Total** | **50** | **+$23,303.18** | **3** | **-$1,840.50** | **+$21,462.68** |

A fill-only correction would take the baseline aggregate from −$12,410.77 to
about **−$33,873**. The new engine's same-cooldown result is −$24,516.45,
which is about **$9.36k better** than that, not worse. The difference is a
second old-engine defect fixed in this phase: a position filled at bar *i+1*'s
open had its stop managed at bar *i*'s close, before the fill existed. A gap
down at the open could therefore set a breakeven stop from the signal bar's
close and stop the new trade out on its entry bar. The new engine skips
management until the entry bar has closed. The same-cooldown comparison
therefore contains both corrections and must not be read as a one-for-one fill
adjustment.

**On XAUUSD alone, 24 impossible stop fills were worth $19,552.20 — more than
the entire +$14,116.92 the old report showed.** That is the single most
important finding of the upgrade: the old XAUUSD number was not a signal edge.

## 5. Frequency

The old cooldown was `int(300 // 60) = 5` **bars** (75 minutes on M15). The
live rule is 300 seconds. The new engine converts the live seconds to bars:

```
M15: ceil(300 / 900) = 1 bar   (15 minutes)
M5:  ceil(300 / 300) = 1 bar   (5 minutes)
```

At zero cost, trades increase from 4,339 to **5,293 (+22.0%)** with the
corrected cooldown. The extra 954 trades are net negative in this sample:
aggregate zero-cost P/L falls from **−$24,516.45** (same cooldown) to
**−$39,519.34**, a change of −$15,002.89. At the same cooldown, trade count is
slightly lower than baseline (4,183 vs 4,339) because the management fix holds
positions longer. Higher frequency is working as specified but is **not** by
itself an edge.

## 6. Cost sensitivity

The cost run is not a small haircut:

* aggregate PF falls from 0.94 (zero cost, new) to **0.72**;
* EURUSD and GBPUSD fall to **PF 0.39 / 0.40**, with win rates near 25%;
* XAUUSD falls from PF 0.99 to 0.93 and loses $23.8k over the sample;
* USDJPY falls from PF 0.89 to 0.55;
* the spread floor is the largest single drag on FX; commission is material on
  both FX and gold.

At the assumed retail costs the current signal stack has **no demonstrated
edge on any of the four symbols**. This is consistent with the earlier
walk-forward failure (7/9 gates) and the native EA tester losses. Nothing is
promoted to live; the dashboard remains in PAPER supervision.

## 7. M5 / M15 support

The research engine now accepts `--timeframe` everywhere:

```powershell
# cache M5 bars + agent votes (new files, does not touch the M15 cache)
python -m research.backtest --symbols XAUUSD --timeframe M5 --days 60 --offline --dump-signals

# replay M5
python -m research.backtest --symbols XAUUSD --timeframe M5 --days 60 --offline `
  --spread-points 20 --commission 7 --slippage-points 2
```

Cache naming: M15 keeps the legacy `state/bt_{symbol}.csv` /
`bt_sig_{symbol}.pkl`; other timeframes use `bt_{symbol}_{TF}.csv` /
`bt_sig_{symbol}_{TF}.pkl`. Sharpe annualisation and the bar-based cooldown
scale with the timeframe. `main.py --timeframe` exposes the same choice for
the legacy CLI and the strategy/walk-forward commands.

**No M5 cache exists in this workspace yet, so the numbers in this report are
M15 only.** M5 is covered by unit tests (paths, cooldown, annualisation) but
must not be inferred from the M15 table.

## 8. Limitations

* The research replay does not simulate the Gemini committee, news filter,
  Kronos (unless `--kronos-stride` is used), guardian, or shared-account
  concurrency. It is a quant-signal + execution study.
* Cached bars are a single broker feed. The demo feed's spread is mostly zero;
  the cost run therefore uses a labelled floor.
* Each symbol is replayed on its own account. The aggregate is a sum, not a
  portfolio; live `max_positions=2` and margin coupling are tested separately
  in the execution suite.
* The same-cooldown comparison mixes the fill correction with the pre-entry
  management correction. It is a before/after upgrade comparison, not an
  isolated attribution of either bug.
* The sample is in-sample for the fixed config. No parameter search was run on
  it in this phase; `research/tune.py` keeps its train/test split.
* A negative result is still a result: the upgrade's job was to make the
  measurement honest, not to guarantee a profitable strategy.

## 9. Reproduction and verification

```powershell
# 1) rebuild the M15 signal cache if state/bt_sig_*.pkl is missing
python -m research.backtest --symbols XAUUSD,EURUSD,GBPUSD,USDJPY --days 120 --dump-signals

# 2) before/after measurement
python -m research.measure_upgrade --baseline-ref a88a920

# 3) the new engine alone (zero-cost and cost runs)
python -m research.backtest --symbols XAUUSD --days 120 --offline --out state/bt_xau_zero.json
python -m research.backtest --symbols XAUUSD --days 120 --offline `
  --spread-points 20 --commission 7 --slippage-points 2 --out state/bt_xau_costs.json

# 4) phase-4 tests
python -m pytest tests/test_research_backtest.py -q
```

The 19 phase-4 tests cover next-bar fills, ask/bid entry and exit,
gap-through stops, SL-before-TP, spread floor, commission drag, round-down
sizing, minimum-lot rejection, fixed-lot risk cap, no management before the
entry bar closes, M5/M15 cooldown and cache paths, extended metrics, and a
no-lookahead equivalence check.

## 10. Verdict

* The upgraded engine is causally correct and materially less optimistic.
* The old XAUUSD backtest profit was dominated by impossible gap fills; a
  fill-only correction puts the old sample near −$33.9k.
* The corrected engine's XAUUSD result is slightly positive only in the
  zero-cost, forced-old-cooldown variant; realistic costs make it negative.
* After realistic costs, none of the four symbols shows an edge in this
  sample (aggregate PF 0.72).
* Keep the system in PAPER/dry-run. The next useful work is signal quality and
  cost-aware exits measured through the live funnel, not another promotion
  attempt.
