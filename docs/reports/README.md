# Phase 3 EA Strategy Tester reports

These are **honest, in-sample smoke-test reports** for the native MQL5 EA
(`TradingAgentsEA`). They prove the EA compiles, trades, manages positions and
writes machine-readable reports inside the MetaTrader 5 Strategy Tester. They
are **not** evidence of an edge and must not be used to promote a strategy to
live trading.

## Runs

| Folder | Period | Tester model | Trades | Win rate | Net R | Profit factor | Max DD |
|---|---|---|---:|---:|---:|---:|---:|
| `phase3_ea_2026-08-01_2026-11-01_ohlc` | 2026-08-01 → 2026-11-01 | OHLC (M15) | 202 | 36.63% | **-18.66R** | 0.77 | 26.85R / 12.76% |
| `phase3_ea_2026-10-01_2026-11-01_m1_ohlc` | 2026-10-01 → 2026-11-01 | "Every tick" requested; broker supplied M1 OHLC ticks | 65 | 27.69% | **-14.33R** | 0.51 | 15.93R / 7.55% |

Common settings: XAUUSD, M15, deposit 100,000 USD, leverage 1:100, native
tester signals only (no Python), risk 0.5% per trade, one position, max 1.00
lot total, spread cap 80 points, UTC+2 server offset. Commission was zero on
this demo account; swap is included.

Each folder contains the EA's `tester_summary.json`, `tester_summary.csv` and
`tester_trades.csv`. The summary includes trades, wins, win rate, net R,
profit factor, expectancy, Sharpe, drawdown, recovery factor, MFE/MAE, spread
cost estimate, and strategy/session/month breakdowns.

## What the results say

- **No demonstrated edge.** Both periods lose money after costs. Trend is
  roughly break-even (PF 0.97 over three months); momentum and SMC are
  clearly negative in this sample.
- **Breakout was effectively dormant**: one trade in three months. Its
  filters (session range, compression, volume, first close beyond the level)
  are too restrictive for this data. This is a research item, not a pass.
- **Overlap session was the worst** in both runs; New York was close to
  break-even in the three-month run.
- **Execution model matters**: the one-month "every tick" request produced
  only M1 OHLC ticks (7,744 ticks) because real tick data was not available
  for this broker/symbol/period. Results differ materially from the OHLC run,
  so treat both as sensitivity checks, not precise simulations.
- The two-month intermediate run (2026-09-01 → 2026-11-01, 137 trades,
  -15.03R, PF 0.73) was superseded by the three-month archive above.
- `max_drawdown_pct` now uses the real deposit captured in `OnInit`; the
  earlier report inferred it from the final balance and could be slightly
  wrong when the tester force-closes a position at the end.

## Bug found and fixed during this pass

This broker reports `SYMBOL_TRADE_TICK_VALUE = 0.1` for XAUUSD while the
contract is 100 oz and one point is 0.01. The first tester run therefore sized
about 10× too large (0.97 lots instead of ~0.10). `Risk.mqh` now derives
value-per-point from the contract size when the profit currency equals the
account currency, and falls back to the tick value otherwise. Sizing is
validated by the report volumes (roughly 0.03–1.00 lots depending on stop
distance) and by `RiskMoneyFor`.

The same pass also:

- caps a valid order to the remaining `max_total_lots` instead of rejecting it;
- blocks strategy structure exits on the entry bar;
- uses the same contract-size-aware value-per-point for the spread-cost
  estimate.

## Reproduce

1. Copy `mql5/Include/TradingAgents/*.mqh` and
   `mql5/Experts/TradingAgents/TradingAgentsEA.mq5` into the MT5 data folder
   and compile with MetaEditor (`0 errors, 0 warnings`).
2. Use the tester inputs in `mql5/Profiles/Tester/TradingAgentsEA.set`
   (not committed to this folder because the data folder is outside the repo).
3. Run `terminal64.exe /config:<ini>` with the dates/model above.
4. Read `%APPDATA%\MetaQuotes\Terminal\Common\Files\TradingAgents\`.

## Gates still open

- Walk-forward, out-of-sample and holdout validation on 30+ days of M1 data.
- Monte Carlo / parameter-stability checks.
- Real tick data with variable spread and realistic commission.
- Python↔EA demo verification: **passed on 2026-11-11** — `hello`, PAPER
  `dry_run` ack, EA-side duplicate rejection, bridge restart/reconnect and
  zero-mismatch reconciliation. Evidence:
  [`phase3_ea_live_smoke_2026-11-11.md`](phase3_ea_live_smoke_2026-11-11.md).
  Still open: mid-trade kill/recovery drills and the per-restart socket
  whitelist on terminal build 6316.
- Only after those pass should any engine be considered for live execution.
