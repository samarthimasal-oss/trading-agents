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
| 4 | `578f636` | Research backtest realism: next-bar fills, bid/ask spread, gap-through fills, SL-before-TP, round-turn commission, exit slippage, spread floor, no pre-entry stop management, M5/M15 caches and cooldown scaling, extended metrics, before/after measurement |
| 5 | this branch tip | Live-management parity: 1R partial close with broker-step volume, commission on both legs, forced breakeven, trailing step, optional time stop; measurement rerun |

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
  checked exits against the bar high/low only. It managed a position filled at
  the next open using the **pre-entry** bar's close, and did not model the live
  1R partial close, trailing step or time stop.
* **New** — this tree's engine, with the symbol and timeframe passed
  explicitly, gap fills, costs, no management before the entry bar closes, and
  the live partial/breakeven/trailing/time-stop rules.

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
* at 1R the live rules close 50% (floored to the broker step), charge
  commission on that leg, and force the remaining position to breakeven;
* the stop only moves when the improvement clears
  `trailing_step_atr * ATR` (floored at the broker minimum distance);
* an optional time stop closes the position after the configured minutes;
* the cooldown is `max(1, ceil(seconds / bar_seconds))`, so it is never shorter
  than the live 300-second gap (1 bar on M15, 3 bars on M5).

Management (partial, breakeven, trailing, time stop) is evaluated at the close
of each bar, not intrabar. That is the same conservative convention used for
the rest of the replay.

## 3. Results (M15, 2026-06-22 → 2026-10-26)

`new zero (same cooldown)` forces the old 5-bar cooldown. `new zero` uses the
corrected 1-bar cooldown. `new + costs` adds the spread floor, commission and
slippage above. The same-cooldown comparison is **not** a pure fill-isolation
run: the new engine also fixes pre-entry management and models the live
partial/breakeven rules (see section 4).

| Symbol | Variant | Trades | Net P/L USD | Win rate | PF | Max DD USD |
|---|---|---:|---:|---:|---:|---:|
| XAUUSD | baseline | 1,116 | **+14,116.92** | 36.6% | 1.05 | 12,369.07 |
| XAUUSD | new zero (same cooldown) | 1,053 | **+5,119.46** | 38.9% | 1.02 | 22,756.31 |
| XAUUSD | new zero | 1,292 | +743.74 | 37.8% | 1.00 | 34,436.57 |
| XAUUSD | new + costs | 1,289 | **-15,631.33** | 36.8% | 0.96 | 34,664.01 |
| EURUSD | baseline | 1,066 | -7,380.45 | 34.8% | 0.87 | 9,423.56 |
| EURUSD | new zero (same cooldown) | 1,004 | -5,459.56 | 38.2% | 0.91 | 7,175.30 |
| EURUSD | new zero | 1,286 | -7,317.05 | 37.7% | 0.91 | 7,861.41 |
| EURUSD | new + costs | 1,343 | **-52,648.01** | 25.4% | 0.41 | 52,647.94 |
| GBPUSD | baseline | 1,076 | -12,446.84 | 31.8% | 0.79 | 13,615.29 |
| GBPUSD | new zero (same cooldown) | 1,018 | -10,961.93 | 35.3% | 0.83 | 14,269.50 |
| GBPUSD | new zero | 1,274 | -11,196.08 | 35.7% | 0.86 | 13,850.12 |
| GBPUSD | new + costs | 1,309 | **-52,301.88** | 26.7% | 0.42 | 52,301.84 |
| USDJPY | baseline | 1,081 | -6,700.40 | 33.6% | 0.91 | 7,244.92 |
| USDJPY | new zero (same cooldown) | 1,031 | -9,005.60 | 36.7% | 0.89 | 11,210.10 |
| USDJPY | new zero | 1,291 | -12,842.84 | 35.2% | 0.88 | 15,176.77 |
| USDJPY | new + costs | 1,303 | **-48,827.61** | 27.2% | 0.55 | 49,042.22 |
| **Aggregate** | baseline | 4,339 | **-12,410.77** | 34.2% | 0.97 | — |
| **Aggregate** | new zero (same cooldown) | 4,106 | **-20,307.63** | 37.3% | 0.96 | — |
| **Aggregate** | new zero | 5,143 | -30,612.23 | 36.6% | 0.95 | — |
| **Aggregate** | new + costs | 5,244 | **-169,408.83** | 28.9% | 0.75 | — |

Aggregate per-trade expectancy: baseline −$2.86, new zero (same cooldown)
−$4.95, new zero −$5.95, new + costs −$32.31.

XAUUSD is the only positive cell in the zero-cost rows: +$5,119.46 at the
forced 5-bar cooldown and +$743.74 at the corrected 1-bar cooldown. After costs
it is −$15,631.33 (PF 0.96). The positive zero-cost result is not enough to
survive the cost assumptions.

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
about **−$33,873**. The new engine's same-cooldown result is −$20,307.63,
about **$13.57k better** than that. The difference comes from the other
old-engine defects fixed in this phase: no management of a next-bar fill with
the pre-entry close, plus the live 1R partial close and forced breakeven. The
same-cooldown comparison therefore contains several corrections and must not
be read as a one-for-one fill adjustment.

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

At zero cost, trades increase from 4,339 to **5,143 (+18.5%)** with the
corrected cooldown. Relative to the same-cooldown run (4,106 trades), the
cooldown fix adds **1,037 trades** and changes aggregate zero-cost P/L from
**−$20,307.63** to **−$30,612.23**, a change of −$10,304.60. At the same
cooldown, trade count is slightly lower than baseline because the live partial
close keeps the position open longer. Higher frequency is working as specified
but is **not** by itself an edge.

## 6. Cost sensitivity

The cost run is not a small haircut:

* aggregate PF falls from 0.95 (zero cost, new) to **0.75**;
* EURUSD and GBPUSD fall to **PF 0.41 / 0.42**, with win rates near 25–27%;
* XAUUSD falls from PF 1.00 to 0.96 and loses $15.6k over the sample;
* USDJPY falls from PF 0.88 to 0.55;
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
  Kronos (unless `--kronos-stride` is used), guardian, max-spread gate, trading
  hours, or shared-account concurrency. It is a quant-signal + execution study.
* Management is evaluated at bar closes. Intrabar partial fills, breakeven and
  trailing moves are approximated by the close; the direction of that
  approximation is not guaranteed.
* Cached bars are a single broker feed. The demo feed's spread is mostly zero;
  the cost run therefore uses a labelled floor.
* Each symbol is replayed on its own account. The aggregate is a sum, not a
  portfolio; live `max_positions=2` and margin coupling are tested separately
  in the execution suite.
* The same-cooldown comparison mixes the fill correction, the pre-entry
  management correction, and partial-close parity. It is a before/after
  upgrade comparison, not an isolated attribution of any one bug.
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

# 4) research-backtest tests
python -m pytest tests/test_research_backtest.py -q
```

The 24 research-backtest tests cover next-bar fills, ask/bid entry and exit,
gap-through stops, SL-before-TP, spread floor, commission drag, round-down
sizing, minimum-lot rejection, fixed-lot risk cap, no management before the
entry bar closes, partial close at 1R, forced breakeven, commission on both
legs, trailing step, time stop, M5/M15 cooldown and cache paths, extended
metrics, and a no-lookahead equivalence check.

## 10. Verdict

* The upgraded engine is causally correct and materially less optimistic.
* The old XAUUSD backtest profit was dominated by impossible gap fills; a
  fill-only correction puts the old sample near −$33.9k.
* The corrected engine's XAUUSD result is slightly positive only before costs
  (+$743.74 at the corrected cooldown); realistic costs make it −$15.6k.
* After realistic costs, none of the four symbols shows an edge in this
  sample (aggregate PF 0.75).
* Keep the system in PAPER/dry-run. The next useful work is signal quality and
  cost-aware exits measured through the live funnel, not another promotion
  attempt.
