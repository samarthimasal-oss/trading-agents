# Python ↔ MetaTrader 5 EA protocol (v1)

This document is the contract between the Python control plane and the native
MQL5 Expert Advisor (`TradingAgentsEA.mq5`). The EA can trade and manage
positions on its own; Python is an optional intent source, never the only
safety layer.

## 1. Scope

- Python **server**: binds `127.0.0.1:8765` (configurable), accepts one EA
  connection, sends intents, receives acknowledgements and position updates.
- EA **client**: connects out to the Python server, authenticates, sends
  heartbeats, validates every intent against broker and risk rules, executes
  through `CTrade`, and manages open positions tick-by-tick.
- The EA also has a `TESTER` mode with a deterministic native signal engine.
  It does **not** use sockets in the Strategy Tester and never waits for
  Python.

## 2. Transport and framing

| Property | Value |
|---|---|
| Transport | TCP over loopback only |
| Python role | listener (`SO_REUSEADDR`) |
| EA role | connector |
| Framing | one UTF-8 JSON object per line (`\n`) |
| Maximum frame | 1 MiB; larger frames are rejected and the connection is closed |
| Default endpoint | `127.0.0.1:8765` |
| Concurrency | one authenticated EA per server; a new authenticated EA replaces a stale one |

Sockets are unavailable inside the MetaTrader Strategy Tester, so the EA
checks `MQL_TESTER` and selects the native tester engine instead.

### 2.1 Terminal whitelist prerequisite

`SocketConnect` is gated by the terminal's WebRequest list. Before the first
attach:

1. **Tools → Options → Expert Advisors**.
2. Enable **Allow WebRequest for listed URL**.
3. Add `127.0.0.1` (host only, no scheme or port; this is the address passed to
   `SocketConnect`). On build 6316, adding only `http://127.0.0.1:8765` did
   not authorize `SocketConnect`; the host-only row is required.
4. Double-click the empty row below the last entry, type `127.0.0.1`, press
   **Enter**, then click **OK**.

The list is encrypted in `config/common.ini`; writing a plaintext URL into
`WebRequestUrl` is ignored. On the tested build 6316, the UI-added entry took
effect immediately in memory but was **not persisted**: `common.ini` was not
rewritten, and a clean terminal restart dropped the entry. Re-add the
host-only row after every terminal restart. A startup INI containing
`[Experts] WebRequestUrl=127.0.0.1` did not seed the list either.
Error **4014** means the address is not allowed; **5272** means the whitelist
is active but nothing is listening.

## 3. Authentication

- A shared token is generated with `python main.py ea-token`.
- Python reads `TRADING_AGENTS_EA_TOKEN` first, then `ea.token_file`
  (default `state/ea_token.txt`).
- The EA reads `TradingAgents\token.txt` from the MQL5 **common** files folder
  (`FILE_COMMON`). The installer script copies it there.
- The token is sent in every envelope and compared in constant time on both
  sides (`hmac.compare_digest` in Python, byte-wise XOR loop in MQL5).
- A token mismatch closes the connection and is logged without the token value.
- The server binds loopback only. There is no TLS because traffic never leaves
  the machine; if the control plane is ever moved off-host, add TLS and an
  authenticated key exchange first.

## 4. Envelope

```json
{"v":1,"type":"intent","id":"py-42","seq":42,"ts":1793633400.123,
 "token":"<hex>","payload":{...}}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | int | protocol version, currently `1` |
| `type` | string | message type (section 6) |
| `id` | string | sender-unique message id, used for request/response correlation |
| `seq` | int | monotonically increasing per sender, starting at 1 for each connection |
| `ts` | number | Unix epoch seconds, UTC; sub-second precision allowed |
| `token` | string | shared secret; never logged |
| `payload` | object | type-specific body; may be `{}` |

Rules:

1. Every field is required.
2. `seq` must increase by exactly one. A gap triggers `resync`; a duplicate or
   old sequence number is ignored.
3. Clock skew is measured against the receiver's UTC clock. More than 2 s emits
   a warning; more than 10 s rejects new entries until corrected.
4. A malformed frame produces `reject` (if the connection is authenticated) or
   closes the connection (if it is not).

## 5. Heartbeats and failure behaviour

- EA heartbeat: every `InpHeartbeatMs` (default 1000 ms).
- Python heartbeat: every 2 s.
- Python marks the EA stale after 5 s without a heartbeat.
- The EA blocks **new entries** after `InpDisconnectGraceSec` (default 30 s)
  without a Python heartbeat, but keeps managing SL/TP, breakeven, trailing,
  partials and time stops.
- If Python dies, existing positions stay protected by broker-side SL/TP and
  the EA's own manager.
- If the EA dies, Python stops sending and marks the bridge disconnected.
  Positions remain protected by broker-side SL/TP.
- On reconnect, both sides perform a full reconciliation before new entries.

## 6. Message types

### `hello`

Sent by both sides immediately after connecting. The EA includes account and
build information. Python replies with its role and session id.

```json
{"role":"ea","version":"1.0.0","account":5057915388,
 "server":"MetaQuotes-Demo","symbol":"XAUUSD","period":"M15",
 "mode":"LIVE","dry_run":false,"magic":20261102,
 "terminal_build":500,"utc_offset_seconds":7200}
```

Python reply:

```json
{"role":"server","version":"1.0.0","session_id":"...",
 "server_time":1793633401.5,"heartbeat_ms":2000}
```

### `heartbeat`

Either side. EA payload:

```json
{"equity":100149.78,"balance":100149.78,"margin_free":100000.0,
 "positions":1,"locked":false,"lock_reason":"","day_r":-0.4,
 "last_bar_time":1793632500,"uptime_seconds":3600,
 "pending_intents":0}
```

Python payload:

```json
{"server_time":1793633401.5,"desired_mode":"live","halted":false}
```

### `intent` (Python → EA)

```json
{"idempotency_key":"<sha256 hex>","symbol":"XAUUSD","direction":1,
 "volume":0.10,"entry":0.0,"sl":3935.20,"tp":3975.40,
 "strategy":"trend","comment":"TA:trend","risk_percent":0.5,
 "max_slippage_points":30,"expires_at":1793634000}
```

| Field | Required | Notes |
|---|---|---|
| `idempotency_key` | yes | `sha256(strategy|symbol|bar_time|direction)`, hex |
| `symbol` | yes | exact broker symbol (suffix included) |
| `direction` | yes | `1` buy, `-1` sell |
| `volume` | yes | EA normalises to broker step/min/max; `0` means size from risk |
| `entry` | no | `0`/omitted means current market |
| `sl` | yes | broker-side stop loss, positive price |
| `tp` | yes | broker-side take profit, positive price |
| `strategy` | yes | `trend`, `breakout`, `smc`, `momentum`, `mean_reversion` |
| `risk_percent` | no | overrides the EA default only downward |
| `expires_at` | no | Unix seconds; expired intents are rejected |
| `max_slippage_points` | no | EA uses the smaller of this and its own cap |

The EA re-validates everything: symbol, price, stops/freeze level, spread,
session, daily lockout, exposure, volume, margin, duplicate key and clock skew.
It never blindly trusts Python.

### `ack` (EA → Python)

```json
{"idempotency_key":"...","status":"filled","ticket":123456789,
 "price":3948.11,"volume":0.10,"retcode":10009,
 "retcode_name":"TRADE_RETCODE_DONE","slippage_points":2.0,
 "message":"","timestamp":1793633404}
```

`status` is one of:

| Status | Meaning |
|---|---|
| `filled` | order accepted and a position exists |
| `duplicate` | key seen within 24 h; returns the original ticket if known |
| `rejected` | validation or broker rejection; `retcode`/`message` explain |
| `dry_run` | validated but not sent because the EA is in paper/dry-run mode |
| `unknown` | send timed out or result ambiguous; **never retry blindly** — reconcile first |

### `reject` (EA → Python)

Used when a whole message cannot be processed (bad JSON, sequence gap, invalid
token already handled at connection level).

```json
{"id":"py-42","reason":"sequence gap","expected_seq":43,"received_seq":45}
```

### `position_update` (EA → Python)

Sent when a managed position is opened, changed (SL/TP/volume) or closed.
`positions` is an array so a snapshot can be sent atomically.

```json
{"positions":[{"ticket":123456789,"symbol":"XAUUSD","direction":1,
 "volume":0.10,"entry":3948.11,"sl":3935.20,"tp":3975.40,
 "price":3952.00,"profit":39.00,"swap":0.0,"opened_at":1793633404,
 "strategy":"trend","comment":"TA:trend","mfe_r":0.8,"mae_r":-0.2,
 "partial_done":false,"breakeven_done":false}]}
```

### `close_request` (Python → EA)

```json
{"ticket":123456789,"idempotency_key":"close-...","reason":"committee exit"}
```

The EA closes the position if it belongs to its magic. If the ticket is gone,
it acks `rejected` with `retcode` `TRADE_RETCODE_POSITION_CLOSED` or a
descriptive message.

### `reconcile_request` / `reconcile_result`

Python sends its view; the EA returns broker truth.

```json
{"since":1793630000,"positions":[{"ticket":123456789,"volume":0.10}],
 "pending_keys":["..."]}
```

```json
{"broker_positions":[...],"broker_orders":[...],"account":{...},
 "mismatches":[{"kind":"missing_at_broker","ticket":123456789}],
 "unknown_keys":["..."],"server_time":1793633405}
```

The EA also messages `position_update` after reconciliation.

### `halt` / `resume` (Python → EA)

```json
{"reason":"daily loss lockout","flatten":true,"until":1793678400}
```

`halt` blocks new entries immediately. `flatten=true` closes all EA positions
with `close_all`. `resume` clears the halt only if the EA's own risk checks
also pass.

### `resync`

Sent by either side after a sequence gap. The receiver replies with a
`heartbeat` snapshot and, for the EA, a `position_update` snapshot.

## 7. Idempotency

- Key = `sha256(f"{strategy}|{symbol}|{bar_time}|{direction}")`.
- The EA persists seen keys with timestamps for at least 24 hours in
  `TradingAgents\seen_keys.csv` (common files).
- A duplicate key returns `status:"duplicate"` and the original ticket if it is
  still known.
- Python keeps a pending-key set and refuses to send the same key twice while an
  ack is outstanding.
- A timeout produces `UNKNOWN`, never an automatic retry. The caller must call
  `reconcile()` and inspect broker positions before deciding.

## 8. Restart and reconciliation

On startup the EA:

1. Loads the token, seen-key cache, daily equity state and position state.
2. Scans broker positions with its magic.
3. Rebuilds per-ticket state (initial risk, partial/BE flags) from its state
   file where possible; unknown positions are marked `adopted` and use the
   current SL as the initial risk (conservative).
4. Sends `hello`, then `position_update` and `reconcile_result` once Python is
   authenticated.

Python reconciles when:

- it reconnects to the EA;
- it receives `unknown` or `rejected` with an ambiguous retcode;
- it starts after being offline;
- a sequence gap is detected.

Resolution rules:

| EA | Broker | Action |
|---|---|---|
| present | present | update state |
| present | missing | mark closed; fetch deal history |
| missing | present | adopt into Python state; alert |
| missing | missing | mark closed; no action |

## 9. Retcodes and filling

The EA uses `CTrade` and reports the raw `retcode` plus a readable name.
It selects the symbol's supported filling mode in this order: FOK, IOC, RETURN.
It normalises prices to `SYMBOL_DIGITS`, volumes to `SYMBOL_VOLUME_STEP`, and
checks `SYMBOL_TRADE_STOPS_LEVEL` and `SYMBOL_TRADE_FREEZE_LEVEL` before send.
Broker-side SL and TP are always attached to the order.

## 10. Quick start (paper mode)

```powershell
# 0. Allow http://127.0.0.1:8765 under Tools > Options > Expert Advisors
#    (see 2.1), then restart MT5.

# 1. Generate the shared token (writes state/ea_token.txt and, by default,
#    %APPDATA%\MetaQuotes\Terminal\Common\Files\TradingAgents\token.txt)
python main.py ea-token

# 2. Start the loopback bridge. Leave it running while the EA is attached.
python main.py ea-server

# 3. In MT5: attach TradingAgentsEA to XAUUSD M15 with
#    InpMode = PAPER, InpTokenFile = TradingAgents\token.txt.
```

`PAPER` validates every intent and returns `dry_run` acks without sending an
order. `LIVE` requires the explicit `InpMode = LIVE` input and a fresh Python
heartbeat. The EA never sends an order while the token is missing or Python is
stale beyond `InpDisconnectGraceSec`.

For a supervised smoke test, run the bridge for a bounded period:

```powershell
python main.py ea-server --seconds 60
```

`python main.py ea-token --force` rotates the token; restart the EA afterwards.
The bridge refuses to bind to a non-loopback host in v1.

## 11. Security checklist

- [x] Bind to `127.0.0.1` only.
- [x] Shared token in every frame, constant-time comparison.
- [x] Token file is outside the repository (`state/` or MQL5 common files).
- [x] Token never logged; messages are logged with the token redacted.
- [x] Maximum frame size and line count bounded.
- [x] Intent keys persisted for 24 h.
- [x] No order is sent in `PAPER`/`DRY_RUN` mode.
- [x] EA validates stops, volume, spread, session, margin and exposure itself.
- [x] `halt`/`resume` are idempotent and auditable.
- [ ] TLS / off-host mode (explicitly out of scope for v1).

## 12. Versioning

`v` is the protocol version. Additive payload fields are backward compatible;
removing or changing a field requires `v=2`. The EA rejects unknown major
versions. The first implementation is `v1`.

## 13. Verification status

Verified:

- [x] EA compiles with 0 errors / 0 warnings (`#property strict`).
- [x] Runs in the Strategy Tester with native signals and no Python.
- [x] Tests cover envelopes, auth, sequence handling, idempotency cache,
  timeouts and reconciliation (`tests/test_ea_client.py`,
  `tests/test_ea_protocol.py`).
- [x] On 2026-11-06 the EA connected to the bridge, authenticated, sent
  `hello`/heartbeats, and completed two `reconcile_result` passes with zero
  mismatches.
- [x] After the bridge stopped, the EA logged the disconnect and reconnect
  attempts returned **5272** (connection refused), confirming the whitelist
  gate was open in that session.
- [x] On 2026-11-11 the full PAPER smoke test passed: a Python `intent`
  produced a `dry_run` ack (`retcode 0`); the same key after clearing only the
  Python ack cache produced `duplicate` (`retcode 10025`); a bridge restart
  led to an EA reconnect and reconciliation with `mismatches: []`,
  `unknown_keys: []` and a flat account (balance = equity = 100,149.78 USD).
  Evidence:
  [`reports/phase3_ea_live_smoke_2026-11-11.md`](reports/phase3_ea_live_smoke_2026-11-11.md).

Not yet verified live (open):

- [ ] Clean terminal restart with the socket whitelist persisted. Build 6316
  requires the host-only `127.0.0.1` row to be re-added after every restart.
- [ ] Kill Python while a position is open; confirm the EA blocks new entries
  after the grace period and keeps managing SL/TP, breakeven and trailing.
- [ ] Kill the EA while a position is open; confirm Python stops sending and
  the position remains protected by broker-side stops.
- [ ] A `LIVE` intent on demo (deliberately not run; keep `InpMode=PAPER`).

Until those pass, keep `InpMode=PAPER` and do not enable live execution.
