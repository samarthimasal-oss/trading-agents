# Phase 2 walk-forward and holdout validation

**Run date:** 2026-11-12  
**Verdict:** **FAIL** — 2 of 9 gates passed. No engine is validated for live
execution; the EA remains PAPER/dry-run.

This report is deliberately negative. The fixed rule set did not produce a
positive out-of-sample edge on this dataset, and the untouched holdout also
lost money. Nothing here authorises live trading.

## Method

- **Dataset:** XAUUSD M15, 12,000 closed bars, 2026-05-06 05:00 → 2026-11-12
  16:00 UTC, exported from the MetaQuotes demo feed (login `5057915388`).
- **Folds:** four contiguous expanding windows. Each test window is
  2,197–2,198 bars. The first test window starts after 210 warmup bars.
- **Holdout:** the final 25% (3,000 bars) is evaluated exactly once and is
  never used to choose a parameter, variant or strategy.
- **Attribution:** a trade belongs to the window in which it was **entered**.
  A position may resolve after a fold boundary (up to 210 tail bars, capped at
  the holdout start), but no decision uses future bars.
- **Fixed rules:** no parameter fitting happens on the folds. This measures
  temporal stability of the five engines, not learned generalisation.
- **Costs:** 20 points spread + 2 points slippage per side (XAUUSD point 0.01).
- **Concurrency:** one position at a time, so the sample is not inflated by
  stacked correlated entries.
- **Monte Carlo:** 2,000 bootstrap resamples of the OOS trade R-multiples,
  seed 42. Trades are treated as independent; that assumption is optimistic.

## Folds

| Fold | Test window (UTC) | Bars | Trades | Win % | Total R | Expectancy | PF | Max DD |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2026-05-08 13:30 → 2026-06-12 14:15 | 2,198 | 88 | 33.0% | **-9.77R** | -0.111R | 0.76 | 20.71R |
| 2 | 2026-06-12 14:15 → 2026-07-17 19:15 | 2,198 | 71 | 42.3% | **-1.33R** | -0.019R | 0.95 | 5.90R |
| 3 | 2026-07-17 19:15 → 2026-08-21 18:30 | 2,197 | 84 | 42.9% | **+6.85R** | +0.082R | 1.22 | 5.23R |
| 4 | 2026-08-21 18:30 → 2026-09-25 19:15 | 2,197 | 84 | 34.5% | **-2.63R** | -0.031R | 0.93 | 10.06R |

Only one of four folds was positive. Fold 1 alone accounts for most of the
loss and a 20.71R drawdown.

## Out-of-sample aggregate

| Strategy | Trades | Win % | Total R | Expectancy | PF | Max DD | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| trend | 111 | 39.6% | **+16.92R** | +0.152R | 1.40 | 5.10R | 1.47 |
| breakout | 0 | — | 0.00R | 0.000R | 0.00 | 0.00R | 0.00 |
| smc | 99 | 31.3% | **-17.85R** | -0.180R | 0.54 | 19.98R | -2.30 |
| momentum | 57 | 43.9% | **+1.76R** | +0.031R | 1.09 | 7.89R | 0.25 |
| mean_reversion | 60 | 40.0% | **-7.70R** | -0.128R | 0.79 | 9.77R | -0.89 |
| **COMBINED** | **327** | **37.9%** | **-6.87R** | **-0.021R** | **0.95** | **22.62R** | **-0.38** |

The frequency target was met (327 trades over roughly 140 trading days, about
2.3/day), but frequency is not edge. Trend carried the portfolio; SMC and mean
reversion more than offset it.

## Untouched holdout

| Strategy | Trades | Win % | Total R | Expectancy | PF | Max DD | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| trend | 32 | 34.4% | -1.38R | -0.043R | 0.91 | 5.60R | -0.23 |
| breakout | 0 | — | 0.00R | 0.000R | 0.00 | 0.00R | 0.00 |
| smc | 36 | 30.6% | -1.44R | -0.040R | 0.90 | 4.03R | -0.23 |
| momentum | 21 | 28.6% | +0.40R | +0.019R | 1.04 | 4.55R | 0.08 |
| mean_reversion | 13 | 23.1% | -6.11R | -0.470R | 0.40 | 6.62R | -1.61 |
| **COMBINED** | **102** | **30.4%** | **-8.52R** | **-0.084R** | **0.83** | **14.71R** | **-0.79** |

Trend, the only positive OOS engine, failed the holdout. Momentum stayed
marginally positive but with a PF of 1.04 on 21 trades, which is noise-level.

## Monte Carlo

| Metric | 5th percentile | Median | 95th percentile |
|---|---:|---:|---:|
| Total R | -35.66R | **-6.80R** | +21.64R |
| Expectancy | -0.109R | **-0.021R** | +0.066R |
| Max drawdown | — | 22.78R | 43.54R |

- `P(total R > 0) = 35.2%`
- `P(profit factor > 1) = 35.2%`
- `P(max drawdown > 10R) = 97.5%`

The bootstrap distribution is centred below zero. This is not a small sample
that happened to lose; it is a distribution whose median outcome is negative.

## Parameter sensitivity

Ten pre-declared variants, evaluated on the folds only (the holdout was not
used):

| Variant | Trades | Total R | Expectancy | PF |
|---|---:|---:|---:|---:|
| baseline | 327 | -6.87R | -0.021R | 0.95 |
| min_rr_0.6 | 327 | -6.87R | -0.021R | 0.95 |
| min_rr_1.0 | 324 | -1.34R | -0.004R | 0.99 |
| min_rr_1.5 | 303 | **+1.78R** | +0.006R | 1.01 |
| min_conf_0.35 | 327 | -6.87R | -0.021R | 0.95 |
| min_conf_0.55 | 321 | -4.78R | -0.015R | 0.96 |
| max_bars_12 | 384 | -3.81R | -0.010R | 0.97 |
| max_bars_40 | 327 | -6.87R | -0.021R | 0.95 |
| atr_stop_0.8x | 329 | -8.31R | -0.025R | 0.94 |
| atr_stop_1.2x | 323 | -3.67R | -0.011R | 0.97 |

Only 1 of 10 variants was positive, and the best (`min_rr_1.5`) is +1.78R over
303 trades with PF 1.01 — indistinguishable from zero. Three variants are
identical to baseline because the engine-level thresholds already bind; that is
itself useful evidence that loosening the arbiter or the confidence floor does
not create trades. No variant was selected for promotion.

## Gates

| Gate | Result | Detail |
|---|---|---|
| `oos_trades` | **PASS** | 327 >= 30 |
| `oos_expectancy` | **FAIL** | -0.021R >= 0.020R |
| `oos_profit_factor` | **FAIL** | 0.95 >= 1.10 |
| `fold_consistency` | **FAIL** | 1/4 folds positive (25%) >= 60% |
| `mc_positive_probability` | **FAIL** | P(total R > 0) = 35.2% >= 90% |
| `mc_drawdown` | **FAIL** | p95 max DD 43.54R <= 10.00R |
| `sensitivity_positive_share` | **FAIL** | 1/10 variants positive (10%) >= 60% |
| `holdout_trades` | **PASS** | 102 >= 10 |
| `holdout_expectancy` | **FAIL** | -0.084R > 0R |

**VERDICT: FAIL (2/9 passed, 7 failed, 0 skipped).**

A PASS would be necessary but not sufficient. A FAIL is decisive: no engine is
promoted, and the holdout must not be reused to tune the rules.

## Findings

1. **No combined edge.** The OOS expectancy is -0.021R and the holdout
   expectancy is -0.084R after modelled costs.
2. **Trend is the only positive OOS engine** (+16.92R, PF 1.40), but it lost
   money in the holdout (-1.38R, PF 0.91). Its apparent edge is not stable
   across the full sample.
3. **SMC is the largest drag** (-17.85R OOS, PF 0.54) and stayed negative in
   the holdout (-1.44R, PF 0.90). Its filters or structure assumptions need
   review before it contributes.
4. **Mean reversion is negative in both** (-7.70R OOS, -6.11R holdout) with
   the worst holdout PF (0.40).
5. **Momentum is approximately flat** (+1.76R OOS, +0.40R holdout); the sample
   is too small and the PF too close to 1 to claim an edge.
6. **Breakout was crowded out of the combined portfolio.** It executed zero
   trades in the combined run despite emitting signals. Run alone through the
   same folds it produced 5 OOS trades (+2.03R, PF 2.00) and one holdout trade
   (-1.01R). The single-position cap plus arbiter ranking explains the zero;
   five trades is far too small to evaluate. It is not a bug, but it is a
   portfolio-design issue.
7. **The Monte Carlo and sensitivity results agree.** The median bootstrap is
   negative and only one of ten parameter variants is positive. The failure is
   not just one unlucky fold.

## Breakout diagnostic

The combined run executed no breakout trades. To distinguish a broken engine
from portfolio competition, the same folds were run with `--only breakout`:

| Scope | Trades | Total R | Expectancy | PF |
|---|---:|---:|---:|---:|
| OOS folds | 5 | +2.03R | +0.405R | 2.00 |
| Holdout | 1 | -1.01R | -1.014R | 0.00 |

The engine does fire, but rarely. With one position slot and roughly 2.3 other
trades per day, its signals are usually blocked or out-ranked. The five-trade
sample cannot support a conclusion in either direction.

## Caveats

- **OHLC bars, not real ticks.** Intrabar path is ambiguous; the replay assumes
  stop-before-target on ties, which is conservative. It is not a substitute for
  a variable-spread real-tick MT5 tester pass.
- **Modelled costs.** The demo account reported zero commission. Spread and
  slippage are charged on both sides, but real execution can be worse.
- **Bootstrap assumes iid trades.** Overlapping strategies and clustered
  regimes violate that assumption, so the interval is indicative.
- **Portfolio-dependent results.** The one-position cap means strategy-level
  results are not independent; breakout's zero is a selection effect.
- **Limited regime coverage.** Six months covers one summer and one autumn, not
  a full market cycle.
- **No fitting.** The rules are fixed. A fitted model could look better in
  sample, but it would need its own untouched out-of-sample test before any
  promotion.
- **Feed-specific.** Results depend on this broker's XAUUSD feed, spread
  behaviour and the UTC session mapping.

## Reproduce

Exact archived dataset:

```powershell
python main.py walkforward --csv docs\reports\xauusd_m15_12000_2026-11-12.csv `
  --symbol XAUUSD --folds 4 --holdout 0.25 --mc-runs 2000 --seed 42 `
  --json docs\reports\walkforward_2026-11-12.json
```

Fresh MT5 fetch (the 12,000-bar window slides with the clock, so results can
differ slightly):

```powershell
python main.py walkforward --bars 12000 --folds 4 --holdout 0.25 --mc-runs 2000 --seed 42
```

Breakout-only diagnostic:

```powershell
python main.py walkforward --csv docs\reports\xauusd_m15_12000_2026-11-12.csv `
  --symbol XAUUSD --only breakout --no-sensitivity --mc-runs 200
```

Artifacts:

- Dataset: [`xauusd_m15_12000_2026-11-12.csv`](xauusd_m15_12000_2026-11-12.csv)
  — SHA-256 `0020ca07f18d3e414afa7c3adec95a1f9cc0561dd210a856165879ad3cf5ae77`
- Full result: [`walkforward_2026-11-12.json`](walkforward_2026-11-12.json)
  — SHA-256 `62eddfcf8b6f555e852064c84ba37e24c441ab6a39715bff74b31c6749909c23`
- Text transcript: [`walkforward_2026-11-12.txt`](walkforward_2026-11-12.txt)

The code path is covered by `tests/test_walkforward.py` (15 tests: split
isolation, entry-time attribution, holdout separation, Monte Carlo determinism,
parameter overrides, gate pass/fail, skipped gates, report sections and the CLI
JSON artifact).

## Decision

- **No engine is promoted.** The EA stays in PAPER/dry-run; the Python
  strategy engines remain research-only and are not wired into live execution.
- **Do not tune on the holdout.** Any rule change needs a new, pre-declared
  validation run on data that includes the current holdout only after the
  change is frozen.
- **Next research steps:** evaluate trend across a longer sample and more
  regimes; review SMC and mean-reversion filters; decide whether the
  one-position cap should be relaxed for genuinely independent setups; and run
  the MT5 tester with real ticks, variable spread and commission before
  revisiting promotion.
