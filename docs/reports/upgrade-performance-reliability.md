# Performance & reliability upgrade — measurement report

**Date:** 2026-11-26<br>
**Last updated:** 2026-11-28 (release verification: 277 tests, frontend build, secret audit, rollback drills, EA compile, bounded dry-run)<br>
**Branch:** `upgrade/performance-reliability`<br>
**Scope:** XAUUSD-first signal reliability, broker-accurate sizing/execution,
real-data funnel analytics, and a causal research backtest. No live promotion.

## 1. What changed

| Phase | Commit | Change |
|---|---|---|
| 1 | `b444e86` | Signal/backtest parity: 300-bar causal window, forming-bar drop, closed-bar indicators/ATR, advisory Gemini semantics (`require_llm_approval=false`), per-symbol LLM rate limit, Kronos ensemble runs, cooldown recorded |
| 2 | `007d947` | Risk/execution: broker-calculated sizing from the actual entry/SL, round **down** to the broker step, reject a minimum lot that would exceed the risk budget, margin/leverage cap, deal-confirmed fills, duplicate/margin/symbol/SL-TP guards, send-error reconciliation, reliable SL modification, fail-closed guardian |
| 3 | `a88a920` | Real-data funnel: `evaluations` journal (one row per stage per symbol per closed bar), `GET /funnel`, Signal Funnel dashboard with rejection reasons and bot-only closed trades |
| 4 | `578f636` | Research backtest realism: next-bar fills, bid/ask spread, gap-through fills, SL-before-TP, round-turn commission, exit slippage, spread floor, no pre-entry stop management, M5/M15 caches and cooldown scaling, extended metrics, before/after measurement |
| 5 | `a71bb1d` | Live-management parity: 1R partial close with broker-step volume, commission on both legs, forced breakeven, trailing step, optional time stop; measurement rerun |
| 6 | this branch tip | Cost-aware tuning: chronological train/validation/holdout split, labelled costs, frequency floor, validation-only selection, one-shot holdout gates, and a primary-symbol gate. The study found no promotable change; its aggregate winner had actually worsened XAUUSD |
| 7 | this branch tip | Reproducibility: every research report embeds a manifest with data, config, signal-cache, cost and code hashes; fixed the offline backtest CLI crash in the agent-scoring block |

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

### Reproducibility manifest

Reports written by `research/backtest.py`, `research/tune.py` and
`research/measure_upgrade.py` now embed a `manifest` block. It records:

* `data.<symbol>.sha256` — hash of the replayed OHLCV/spread fields, plus the
  relative source CSV, byte size and source-file hash;
* `inputs` — hashes of the signal cache and `state/bt_settings.json`;
* `config.sha256` — hash of the full config (values are never written);
* `code.commit`, `dirty` and `source_sha256` — git identity plus a source-tree
  hash that also covers uncommitted edits;
* `costs`/`costs_sha256`, `split`, run `parameters`, and the Python/NumPy/pandas
  versions.

Check a report against the current checkout with:

```python
import json
from research import manifest
report = json.load(open("state/backtest_report.json"))
data = report["manifest"]["data"]["XAUUSD"]
print(manifest.sha256_file("state/bt_XAUUSD.csv") == data["source_sha256"])
```

A report is reproducible only when the data and source hashes match;
`dirty: true` identifies an uncommitted tree rather than a commit. While adding
this, the offline backtest CLI crashed before writing its report because the
agent-scoring block referenced an undefined local. The block now filters
per-agent samples through `symbol_agent_scores()`, with a regression test.

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
* The full-sample measurement is in-sample for the fixed config. The separate
  cost-aware study in section 10 uses a chronological holdout; its validation
  improvement did not survive the holdout.
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

# 4) cost-aware train/validation/holdout study
python -m research.tune --timeframe M15 --offline

# 5) research and tuner tests
python -m pytest tests/test_research_backtest.py tests/test_tuning.py -q
```

The 24 research-backtest tests cover next-bar fills, ask/bid entry and exit,
gap-through stops, SL-before-TP, spread floor, commission drag, round-down
sizing, minimum-lot rejection, fixed-lot risk cap, no management before the
entry bar closes, partial close at 1R, forced breakeven, commission on both
legs, trailing step, time stop, M5/M15 cooldown and cache paths, extended
metrics, and a no-lookahead equivalence check. The 23 tuner tests cover split
boundaries/non-overlap, exit variants, cost overrides, frequency floors,
validation-only selection, holdout-once evaluation, the profit and
primary-symbol gates and the gated config write. The manifest tests cover hash
stability, secret-free config hashing, relative paths and missing inputs. The
full Python suite is **267 tests**.

## 10. Cost-aware signal study (train / validation / holdout)

`research/tune.py` was rewritten before this study. The old version selected
its “winner” on the same later window it reported, ignored costs, had no
frequency floor and applied config changes without a holdout. The new version
uses three contiguous, non-overlapping windows and never lets the holdout
influence selection.

Command:

```powershell
python -m research.tune --timeframe M15 --offline
```

Split (XAUUSD dates shown; all symbols use the same bar indices):

| Window | Dates | Bars per symbol |
|---|---|---:|
| Train | 2026-06-25 12:15 → 2026-08-26 06:15 | 3,838 |
| Validation | 2026-08-26 06:30 → 2026-09-25 03:15 | 1,918 |
| Holdout | 2026-09-25 03:30 → 2026-10-26 19:45 | 1,918 |

The 300-bar warmup and the last 6 bars are outside the three windows. The
search grid is fixed in advance: 5 weight presets (live plus trend-heavy,
momentum-heavy, reversion-heavy and balanced) × 3 consensus thresholds
(0.20/0.30/0.40) × 2 dead-market ratios (0.62/0.75) × 4 exit variants (live,
no-partial, 4.0×ATR target, no trailing). After removing the duplicate of the
live config, **119 challengers plus the live baseline** were tested.

The study reuses the cached per-bar votes of the five quant agents. It tests
reweighting, threshold, dead-market and exit changes; it does **not** retune
the agents' indicator parameters and does **not** include Kronos or Gemini.
Those remain part of the live path and are not covered by this holdout
comparison.

Every replay pays the same labelled costs as section 2: $7/lot round-turn
commission, 2 points adverse slippage per fill, and spread floors of 20 points
(XAUUSD), 8 (EURUSD), 10 (GBPUSD) and 8 (USDJPY). The frequency floor is
**120 trades per 1,000 symbol-bars**; 89 of the 119 challengers cleared it on
train. The top 12 by train net PnL advanced to validation. Train never chooses
the winner; it only narrows the field.

**Validation selection.** The highest validation net PnL that cleared the
floor was `momentum_hi:thr0.40:dead0.75:live` (weights trend 0.9, momentum 1.3,
reversion 0.3, breakout 1.0, pullback 0.6; threshold 0.40; dead-market 0.75):

| Validation candidate | Trades | Net PnL | PF | USD/trade | Trades/1k bars |
|---|---:|---:|---:|---:|---:|
| Selected: momentum_hi, 0.40, dead 0.75, live exits | 1,112 | **−$40,687.20** | 0.77 | −$36.59 | 144.9 |
| Live baseline | 1,329 | −$51,936.10 | 0.74 | −$39.08 | 173.2 |

The selection looked like an improvement: less negative PnL, a slightly better
PF and a better per-trade result than the live baseline.

**One-shot holdout.** The winner and the live baseline were then replayed once
on the untouched holdout:

| Holdout configuration | Trades | Net PnL | PF | USD/trade | Trades/1k bars |
|---|---:|---:|---:|---:|---:|
| Selected: momentum_hi, 0.40, dead 0.75, live exits | 1,076 | **−$58,527.61** | 0.63 | −$54.39 | 140.3 |
| Live baseline | 1,310 | −$63,907.59 | 0.64 | −$48.78 | 170.8 |

The selected variant lost $5,379.98 less in total only because it traded less.
Its per-trade expectancy and profit factor were **worse** than the baseline,
so the validation improvement did not generalise into a better signal.

**XAUUSD is the primary symbol, and the aggregate hid a regression there.**
Per-symbol holdout results:

| Symbol | Winner trades | Winner net PnL | Winner PF | Winner USD/trade | Baseline net PnL | Baseline PF | Baseline USD/trade |
|---|---:|---:|---:|---:|---:|---:|---:|
| **XAUUSD** | 287 | **−$12,345.95** | 0.83 | **−$43.02** | **−$8,151.12** | 0.89 | **−$25.96** |
| EURUSD | 249 | −$13,825.65 | 0.51 | −$55.52 | −$17,109.96 | 0.50 | −$52.16 |
| GBPUSD | 257 | −$11,401.26 | 0.53 | −$44.36 | −$14,192.34 | 0.51 | −$43.94 |
| USDJPY | 283 | −$20,954.75 | 0.37 | −$74.05 | −$24,454.17 | 0.37 | −$70.88 |
| **Aggregate** | 1,076 | −$58,527.61 | 0.63 | −$54.39 | −$63,907.59 | 0.64 | −$48.78 |

The winner made XAUUSD **$4,194.83 worse** in total and $17.06 worse per
trade than the live baseline. The aggregate improvement came entirely from
EURUSD/GBPUSD/USDJPY trading less; every symbol still lost money. Aggregate-only
gates can therefore approve a change that degrades the primary symbol,
so `research.tune.py` now adds a **primary-symbol gate**: the configured
primary symbol (XAUUSD) must be net positive on the holdout, beat the baseline
on net PnL, and beat it on per-trade expectancy. Per-symbol rows now carry
`avg_pnl_per_trade`, and `_expectancy()` falls back to `pnl/trades` so older
result files are read correctly.

**Per-agent horizon diagnostic.** `python -m research.horizon_edges` (read-only;
fixed 60/40 train/validate split, no costs or holdout) was run to check whether
any agent has a stable directional edge on XAUUSD. None does: trend's
confidence-weighted edge is +0.03 to +0.25 ATR across 2–24 bars on train but
−0.04 to +0.03 ATR on validation; pullback goes from +0.02…+0.11 to
−0.03…−0.23; reversion is negative on train and mostly positive at short
horizons on validation. The signs flip, so the diagnostic does not justify
reweighting an individual agent. It also cannot see costs, so a positive
pre-cost edge at 2–4 bars would not survive the spread floor. This is
consistent with the holdout result: the problem is not the weight vector alone.

| Holdout gate | Result |
|---|---|
| Frequency ≥ 120/1k bars | **PASS** (140.3) |
| Expectancy > $0/trade | **FAIL** (−$54.39) |
| Profit factor ≥ 1.05 | **FAIL** (0.63) |
| Net PnL > $0 | **FAIL** (−$58,527.61) |
| Beats baseline on PnL, expectancy and PF | **FAIL** (−$58.5k vs −$63.9k, −$54.39 vs −$48.78/trade, PF 0.63 vs 0.64) |
| Primary symbol positive and beats baseline | **FAIL** (XAUUSD −$12,345.95 vs −$8,151.12; −$43.02 vs −$25.96/trade) |

`--apply` therefore wrote nothing: `config.json` was left unchanged. The
machine-readable study is `state/tuning_result.json` (generated, untracked);
its `manifest` block records the four CSV and signal-cache hashes, the config
hash, the source tree, costs and the exact split. The old full-sample
measurement is still useful for execution-realism, but it is in-sample for the
fixed config; the holdout table above is the relevant out-of-sample evidence
for parameter changes.

## 11. Verdict

* The upgraded engine is causally correct and materially less optimistic.
* The old XAUUSD backtest profit was dominated by impossible gap fills; a
  fill-only correction puts the old sample near −$33.9k.
* The corrected engine's XAUUSD result is slightly positive only before costs
  (+$743.74 at the corrected cooldown); realistic costs make it −$15.6k.
* After realistic costs, none of the four symbols shows an edge in this
  sample (aggregate PF 0.75).
* The cost-aware holdout study selected a momentum-heavy, higher-threshold
  variant that lost less in total by trading less, but was worse per trade and
  had a lower PF than the live baseline. Every profit gate failed, so no
  setting was applied. Its aggregate improvement also **worsened XAUUSD** by
  $4,194.83, which is why the tuner now gates on the primary symbol.
* The per-agent horizon scan found no stable XAUUSD edge across train and
  validation; agent signs flip, so reweighting alone is not the next fix.
* Research reports now carry a manifest (data/config/signal-cache/code hashes),
  so a result can be tied back to the exact inputs and source tree. The
  offline backtest CLI bug that prevented report writing is fixed.
* Keep the system in PAPER/dry-run. The next useful work is signal quality and
  cost-aware exits measured through the live funnel, not another promotion
  attempt.

## 12. Release verification (2026-11-28)

All checks below were run on this branch. MT5 was connected to the demo account
(login 5057915388, equity 100,142.95) and the system stayed in PAPER/dry-run.

| Check | Command | Result |
|---|---|---|
| Python suite | `python -m pytest` | **277 passed** in 16.5s |
| Coverage | `python -m pytest --cov=core --cov=execution --cov=research --cov=api --cov=runner --cov=main` | **62% total**; risk/execution modules 67–94%, funnel service 93% |
| Frontend suite | `npm run test:run` (web/) | **30 passed** |
| Frontend build | `npm run build` (web/) | **pass** (2,074 modules) |
| Secret audit | `python scripts/audit_secrets.py` | 20 commits, 282 tracked files, 8 logs; **0 credential hits**, 0 sensitive tracked files |
| Rollback drills | `pytest tests/test_rollback.py` | config backup and guardian SQLite restore pass |
| EA compile | `scripts\install_ea.ps1 -Compile` | **0 errors, 0 warnings** (10 includes + EA copied) |
| EA bridge | `python main.py ea-server --seconds 10` | bound `127.0.0.1:8765`, stopped cleanly, exit 0; `ea.enabled=false` |
| MT5 check | `python main.py check` | exit 0; demo account; XAUUSD, EURUSD, GBPUSD, USDJPY valid |
| Bounded dry-run | `python main.py run --dry-run --once` | exit 0; 16 funnel rows; no orders sent |

### Bounded dry-run evidence

`run` now accepts `--seconds N` (deadline) and `--once` (single cycle), so a
smoke test no longer needs an external kill. The 2026-11-28 cycle produced a
complete funnel:

| Symbol | Funnel result |
|---|---|
| XAUUSD | `signal/reject`: quote quarantined, spread 262 pts > 80 pts |
| EURUSD | `signal/reject`: no consensus (strength +0.04) |
| GBPUSD | signal → gemini approve → risk pass → `execution/skip`: dry run |
| USDJPY | signal → gemini advisory veto (ignored because approval is not required) → risk pass → `execution/skip`: dry run |

No real orders were sent. This confirms the dashboard funnel is fed by the live
path, including a real quote quarantine on the primary symbol.

### Still open (requires MT5/EA or additional tooling)

* Attach `TradingAgentsEA` to XAUUSD M15 (`InpMode=PAPER`) and run
  `scripts/ea_live_smoke.py`; the bridge has not had an EA client since
  2026-11-13.
* Verify the MT5 `127.0.0.1` WebRequest entry survives a terminal restart, then
  re-test the handshake/whitelist.
* Demo live reconciliation and a mid-trade rollback drill need an attached EA
  and an open position.
* Total coverage is 62%; `runner.py` (12%) and `main.py` (20%) are exercised
  mainly by the live/CLI paths and have no integration harness yet. Install
  `pytest-cov` before repeating the coverage command.
