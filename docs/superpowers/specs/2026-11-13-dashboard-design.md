# TradingAgents dashboard — design

**Date:** 2026-11-13  
**Branch:** `upgrade/phase-7-dashboard`  
**Status:** direction approved; builder chose the defaults marked “builder’s call”

## Intent

Replace the single-page Flask dashboard with a real control room for the
existing MT5 system: FastAPI backend, React/TypeScript/Tailwind frontend,
WebSocket updates, and nine pages. The first job is **supervising the
PAPER/demo EA and the risk state at a glance**; research depth (committee,
strategies, backtests) supports that job. The dashboard is an observability
and control surface, not a source of trading edge.

The 2026-11-12 walk-forward run failed 7 of 9 gates. Nothing in this dashboard
may imply that an engine is validated or that the system is profitable.

## Goals

- One place to see account, positions, EA/bridge/runner health, risk state,
  recent decisions and open risk, refreshed live.
- Every mutating control performs a real, auditable backend action and
  reports success or a specific error.
- Safe defaults: DRY-RUN/PAPER, explicit confirmation for live/destructive
  actions, secrets never returned.
- Nine pages: Overview, Live Trading, AI Committee, Strategy Desk, Market
  Intelligence, Backtesting, Learning Centre, Risk Centre, Settings.
- Work when data is missing: show “unavailable” with the reason, never mock
  numbers.

## Non-goals

- No new trading strategy, no promotion of the five engines, no live-mode
  enablement by default.
- No replacement of the MT5 EA or its loopback protocol.
- No multi-user accounts, cloud deployment, or remote internet exposure.
- No fabricated closed-trade learning metrics. R, MFE/MAE, strategy and exit
  classification appear only when a real source exists.

## Anti-goals

- A dashboard that shows green numbers for stale or synthetic data.
- Buttons that only change local UI state.
- Returning API keys, tokens or passwords in any response or log.
- A live order path without an explicit confirmation and an audit record.

## Constraints

- Windows 10, Python 3.12, Node 24. Local loopback use only.
- Existing `MT5Client`, `StateStore`, config schema, runner CLI, five strategy
  engines, research replay and walk-forward harness must be reused.
- The Flask dashboard must remain runnable until the React app reaches parity.
- `config.json`, `state/`, tokens, logs and databases stay gitignored.
- The live `config.json` currently has no `strategies` or `ea` section; the
  new engines and EA settings therefore use defaults. The UI must say so.

## Approaches considered

1. **Extend Flask and add a React page inside it.** Cheapest, but the request
   is a FastAPI + React control surface; mixing two servers and two auth
   models would make every control harder to audit.
2. **FastAPI + server-rendered templates (Jinja).** Simple deployment, but
   weak fit for live charts, WebSocket state and the requested stack.
3. **FastAPI API + Vite React SPA, Flask kept as fallback.** Chosen. Clear
   separation, typed contracts, testable endpoints, static build served by
   FastAPI in production, Vite dev proxy in development.

### Builder’s calls

- React 19 + TypeScript + Vite 7 + Tailwind 3.4.
- TanStack Query for server state; a single WebSocket hook for live pushes.
- `lightweight-charts` for candles; Recharts for equity, drawdown and R
  distributions; CSS grids for heatmaps.
- Local bearer token, auto-generated at `state/dashboard_token.txt`, with a
  login screen. Token is never placed in URLs except the WebSocket query
  string, which is loopback-only and not logged.
- Separate `state/dashboard.sqlite` for audit records, settings overrides and
  backtest jobs. The runner’s `agents.sqlite` is read-only to the dashboard.
- Backtests run as subprocess jobs (`python main.py walkforward ...`) with
  JSON output under `state/backtests/`; the API never blocks on a long run.
- All timestamps are UTC in the API; the UI renders local time with a UTC
  label.

## Architecture

```
web/ (React SPA) ──HTTP/WS──> api/ (FastAPI)
                                 ├── services/account, journal, market
                                 ├── services/risk, runner, settings
                                 ├── services/strategies, backtests
                                 ├── services/learning, committee
                                 ├── repository (dashboard.sqlite)
                                 └── MT5Client / StateStore / config / research
```

### Backend surface (versioned under `/api/v1`)

| Area | Endpoints |
|---|---|
| System | `GET /health`, `GET /system/info`, `GET /system/logs` |
| Auth | `POST /auth/login`, `GET /auth/me` |
| Account | `GET /account`, `GET /positions`, `GET /orders` |
| Journal | `GET /decisions`, `GET /events`, `GET /equity` |
| Market | `GET /market/quote`, `GET /market/candles`, `GET /market/overview` |
| Strategies | `GET /strategies`, `PATCH /strategies/{name}`, `POST /strategies/signals`, `POST /strategies/replay` |
| Risk | `GET /risk`, `POST /risk/emergency-close`, `POST /risk/reset-guardian` |
| Runner | `GET /runner`, `POST /runner/start`, `POST /runner/stop` |
| Committee | `GET /committee` |
| Learning | `GET /learning/trades`, `GET /learning/summary` |
| Backtests | `GET /backtests`, `POST /backtests`, `GET /backtests/{id}`, `GET /backtests/{id}/report` |
| Settings | `GET /settings`, `PATCH /settings` |
| Live | `WS /ws?token=...` |

Every response that can fail uses a structured error:
`{ "error": { "code": "...", "message": "...", "detail": ... } }`.

Mutating endpoints require the bearer token, an `X-Dashboard-Request: 1`
header, and an `Idempotency-Key` where the action can create something
(backtest, order, close-all). The audit table records actor, action, payload
hash, result, and timestamp.

### Safety rules

- `POST /risk/emergency-close` defaults to `dry_run: true`; a real close
  requires `confirm: "CLOSE ALL"` and `dry_run: false`.
- Runner start defaults to dry-run. A live start requires
  `confirm: "LIVE"` and is refused unless `trading.require_llm_approval` or
  an explicit `allow_live` setting is true. (Builder’s call: refuse while the
  walk-forward gate is failed.)
- Settings/risk updates are refused with `409` while the runner is active,
  because the runner reads config at startup. The UI explains the required
  restart.
- Strategy enable/disable writes `state/strategy_overrides.json` and applies
  to research replay/signals; the UI labels it “research-only — live uses the
  legacy path”.
- No secret is ever serialized. `GET /settings` returns only allow-listed
  fields and booleans such as `llm_key_present`.

## Data honesty matrix

| Page | Real source | If missing |
|---|---|---|
| Overview | MT5 account/positions, StateStore, runner PID, EA TCP state | “unavailable” badge with reason |
| Live Trading | MT5 positions/orders, decisions/events, equity | empty state, not zeros |
| AI Committee | `state/live.json` LLM fields + decision reviews | “no recent committee review” + age |
| Strategy Desk | registry defaults, overrides, replay signals | “research-only” banner |
| Market Intelligence | MT5 candles, features, structure, Kronos cache | “no forecast available” |
| Backtesting | walk-forward JSON artifacts + job runner | “no runs yet” |
| Learning Centre | MT5 deal history + decisions; missing R/MFE/MAE shown as `—` | explicit unavailable fields |
| Risk Centre | risk config, guardian state, EA state | “guardian state unavailable” |
| Settings | allow-listed config; secrets redacted | read-only with reason |

## Delivery slices

1. **Foundation:** API skeleton, auth, health, account/journal/equity,
   runner/EA status, risk read + dry-run emergency close, settings read,
   WebSocket, React shell/login/Overview/Risk/Settings. Tests for every
   endpoint and the confirmation flow.
2. **Trading surfaces:** Live Trading, AI Committee, Strategy Desk, Market
   Intelligence with real data, replay/signals, charts and error states.
3. **Research surfaces:** Backtesting job manager, Learning Centre,
   E2E Playwright, accessibility/responsive pass, docs and Flask fallback note.

Each slice is committed and pushed only after `pytest`, frontend unit tests,
and a production build pass.

## Testing

- Backend: pytest + FastAPI `TestClient`, dependency overrides for MT5,
  config, store and runner; tests for auth, redaction, idempotency, dry-run
  defaults, 409 rules, job lifecycle and WS auth.
- Frontend: Vitest + React Testing Library for auth gate, error/empty states,
  confirmation dialogs and API client.
- E2E: Playwright against a seeded API fixture; no real orders.
- No test may depend on live MT5, the EA bridge or network access.

## Deferred / open

- Closed-trade learning fields that need the Phase 6 trade store.
- Real tick backtests and commission modelling.
- Token rotation UI (the CLI `ea-token` remains the EA token path).
- Remote access/TLS; explicitly out of scope while loopback-only.
