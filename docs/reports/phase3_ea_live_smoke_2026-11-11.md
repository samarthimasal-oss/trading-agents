# Phase 3 EA live smoke test — 2026-11-11

Supervised verification of the Python ↔ MQL5 loopback protocol on the
MetaQuotes demo account. The EA ran in **PAPER** mode (`dry_run=true`); no
order was sent and the account stayed flat.

## Result

| Check | Result | Evidence |
|---|---|---|
| EA connects and authenticates | **Pass** | `hello` with account, symbol, mode, build |
| PAPER intent returns a dry-run ack | **Pass** | `ACK1 dry_run`, `retcode 0` |
| Duplicate key is rejected by the EA | **Pass** | `ACK2 duplicate`, `retcode 10025` |
| Bridge restart → EA reconnect | **Pass** | second `hello`; EA log `19:45:13` |
| Reconciliation after reconnect | **Pass** | `mismatches: []`, `unknown_keys: []` |
| No order / no position in PAPER | **Pass** | 0 positions; balance = equity = 100,149.78 |
| Whitelist survives terminal restart | **Fail (terminal build 6316)** | entry must be re-added manually; see below |

Raw transcript: [`phase3_ea_live_smoke_2026-11-11.txt`](phase3_ea_live_smoke_2026-11-11.txt).

## Environment

| Item | Value |
|---|---|
| Date | 2026-11-11, ~19:44–19:45 local |
| Terminal | MetaTrader 5 build 6316 |
| Account | `5057915388` (MetaQuotes-Demo, hedge) |
| Symbol / timeframe | XAUUSD / M15 |
| EA mode | `PAPER` (`dry_run=true`) |
| Magic | `20261102` |
| Risk input | 0.50% per trade |
| Bridge | `127.0.0.1:8765`, loopback TCP, newline-delimited JSON |
| Token | `state/ea_token.txt`, copied to the MQL5 common files folder (value not recorded) |

## Test sequence and observed output

The probe used a fresh idempotency key derived from
`sha256("trend|XAUUSD|<bar_time>|1")` (prefix `804211fd70736a01`), a market
buy at `4030.73`, stop `4025.73`, target `4038.73`, `volume=0` (EA sizes from
risk), `risk_percent=0.5`, strategy `trend`.

1. **Start bridge, wait for EA.** The EA sent `hello`:
   `account=5057915388`, `symbol=XAUUSD`, `mode=PAPER`, `dry_run=true`,
   `terminal_build=6316`, `utc_offset_seconds=7200`.
2. **Send the intent.** The EA validated it and returned:
   `status=dry_run`, `ticket=0`, `retcode=0`, `message="dry-run: validated, no order sent"`.
3. **Clear only the Python-side ack cache and resend the same key.** The EA
   returned:
   `status=duplicate`, `ticket=0`, `retcode=10025`,
   `retcode_name=TRADE_RETCODE_NO_CHANGES`,
   `message="duplicate idempotency key"`. This proves the EA's persisted
   seen-key cache, not the Python cache, rejected the duplicate.
4. **Stop the bridge and start a new one.** The EA reconnected and sent a new
   `hello`. `reconcile()` returned
   `mismatches=[]`, `broker_positions=[]`, `unknown_keys=[]`,
   `balance=100149.78`, `equity=100149.78`, `leverage=100`.
5. **Leave a long-running bridge up.** The EA reconnected to the 8-hour bridge
   at `19:45:13` and completed two reconciliation passes with zero mismatches.

EA log excerpt:

```text
19:35:43  TA: connected to Python bridge 127.0.0.1:8765
19:37:17  TA deinit: reason=9 trades=0 netR=0.00
19:39:11  TA init: symbol=XAUUSD tf=PERIOD_M15 mode=PAPER magic=20261102 risk=0.50% maxpos=1 maxspread=80 native=on token=ok
19:40:11  TA: Python bridge unavailable: SocketConnect(127.0.0.1:8765) failed: 4014
19:44:22  TA: Python bridge unavailable: SocketConnect(127.0.0.1:8765) failed: 5272
19:44:47  TA: connected to Python bridge 127.0.0.1:8765
19:44:49  TA: socket disconnected
19:44:51  TA: connected to Python bridge 127.0.0.1:8765
19:44:52  TA: socket disconnected
19:45:01  TA native entry: trend conf=0.79 rr=2.00 SELL @ 4030.62000
19:45:13  TA: connected to Python bridge 127.0.0.1:8765
```

The `19:45:01` line is the EA's own native signal engine producing a PAPER
signal. No position was opened; the account check after the run reported
`positions 0`.

## Socket whitelist finding (build 6316)

`SocketConnect("127.0.0.1", 8765)` requires a **host-only** `127.0.0.1` entry
in **Tools → Options → Expert Advisors → Allow WebRequest for listed URL**.
Adding `http://127.0.0.1:8765` alone did not authorize the socket call.

Working UI sequence:

1. Open **Tools → Options → Expert Advisors**.
2. Double-click the empty row below the last URL in the list.
3. Type `127.0.0.1`, press **Enter**.
4. Click **OK**.

The entry is active in memory immediately, but this terminal build does **not**
persist it:

- `config/common.ini` kept `LastWriteTime 11-11-2026 19:28:17` and
  `Length 2060`; the `WebRequestUrl` field remained an encrypted blob.
- A clean terminal restart dropped the entry and `SocketConnect` returned
  **4014** again.
- A startup INI containing `[Experts] WebRequestUrl=127.0.0.1` did **not**
  seed the list.

Consequence: re-add the host-only entry after every clean terminal restart.
Error **4014** means the address is not allowed; **5272** means the whitelist
is active but the Python bridge is not listening. `5272` at `19:44:22` was the
intermediate state between re-adding the entry and starting the smoke bridge.

This is a terminal-build behaviour, not an EA or protocol defect. It is
tracked as an open operational gate.

## Additional observation

A later rerun at 22:35 local reconnected from a long-idle stale socket, then
correctly returned `rejected` / `10013` with
`outside london/overlap/newyork`. This is the EA session gate working as
designed. It is why the full PAPER cycle must be run during London, overlap or
New York hours.

## Repeat run (2026-11-12)

The committed `scripts/ea_live_smoke.py` repeated the full sequence on
2026-11-12 at 19:18 local (New York session) and printed `SMOKE PASS`:
`ACK1 dry_run` (`retcode 0`), `ACK2 duplicate` (`retcode 10025`), bridge
restart/reconnect, and `mismatches: []` / `unknown_keys: []` with the account
still flat. The raw output is appended to the transcript.

## What this proves

- The EA authenticates with the shared token and reports accurate account,
  symbol, mode and build metadata.
- A valid PAPER intent is re-validated by the EA and acknowledged as
  `dry_run` without sending an order.
- The EA's persisted idempotency cache rejects a duplicate key even after the
  Python-side cache is cleared.
- The EA recovers from a bridge restart, re-authenticates and reconciles
  against broker truth with zero mismatches.
- PAPER mode leaves the account flat.

## What this does not prove

- **No live execution.** No `LIVE` intent, no real order, no broker-side
  SL/TP management on a live position.
- **No mid-trade failure drill.** Python was not killed while a position was
  open; the EA's grace-period entry block and protective management were not
  exercised on a real position.
- **No whitelist persistence.** The entry still has to be re-added after a
  clean terminal restart on build 6316.
- **No profitability claim.** The Strategy Tester evidence remains negative
  (see [`README.md`](README.md)); this smoke test only verifies plumbing.
- **No real-tick / variable-spread / commission tester pass.** The tester
  still supplied M1 OHLC ticks for the one-month run.

## Reproduce

1. `python main.py ea-token` (once; copies the token to the MT5 common files
   folder).
2. Add the host-only `127.0.0.1` whitelist entry and click **OK**.
3. Attach `TradingAgentsEA` to XAUUSD M15 with `InpMode=PAPER`.
4. Run `python scripts/ea_live_smoke.py` during London, overlap or New York
   hours. It starts a bridge, sends a PAPER intent, clears the Python ack
   cache, resends the same key, restarts the bridge, reconciles, and prints
   `SMOKE PASS`. Outside those sessions it prints `SKIP` and exits 2 because
   the EA's session gate rejects new entries.

The script refuses to run if `hello.mode` is not `PAPER` or `dry_run` is false.

## Safety notes

- The account is a demo account and remained flat.
- The token value is not included in this report or the transcript.
- No `config.json`, `state/`, token file, or MT5 `common.ini` content is
  committed.
- Keep `InpMode=PAPER` until the remaining gates (mid-trade failure drill,
  whitelist persistence, real-tick tester, Phase 2 walk-forward/holdout) pass.
