"""Dashboard API foundation: auth, safety rules, redaction, WS and audit."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.repository import DashboardStore
from api.services import ApiSettings, Services
from api.services.settings_service import SettingsService


class FakeMT5:
    def __init__(self):
        self.closed = 0
        self.close_calls = 0

    def health(self):
        return {"ok": True, "detail": "fake terminal"}

    def account(self):
        return {
            "login": 5057915388,
            "balance": 100149.78,
            "equity": 100149.78,
            "margin": 0.0,
            "free_margin": 100149.78,
            "margin_level": 0.0,
            "profit": 0.0,
            "currency": "USD",
        }

    def positions(self, symbol=None, magic=None):
        return [
            {"ticket": 11, "symbol": "XAUUSD", "type": 0, "volume": 0.10,
             "price_open": 3850.0, "sl": 3840.0, "tp": 3870.0,
             "profit": 12.5, "magic": 260101},
            {"ticket": 12, "symbol": "EURUSD", "type": 1, "volume": 0.20,
             "price_open": 1.10, "sl": 1.11, "tp": 1.08,
             "profit": -3.0, "magic": 260101},
        ]

    def orders(self, symbol=None, magic=None):
        return [{"ticket": 21, "symbol": "XAUUSD", "type": 2,
                 "volume_current": 0.10, "price_open": 3845.0}]

    def quote(self, symbol):
        return {"symbol": symbol, "bid": 3859.9, "ask": 3860.0,
                "digits": 2, "point": 0.01, "spread_points": 10.0,
                "time": 1794500000.0}

    def candles(self, symbol, timeframe="M15", bars=96):
        return [{"time": 1794500000.0 - i * 900, "open": 3850.0 + i,
                 "high": 3852.0 + i, "low": 3849.0 + i,
                 "close": 3851.0 + i, "tick_volume": 100.0}
                for i in range(min(bars, 5))]

    def close_all(self, magic=None):
        self.close_calls += 1
        return 2


class FakeJournal:
    def decisions(self, limit=100, symbol=None, since=None):
        rows = [
            {"ts": 1794490000.0, "symbol": "XAUUSD", "action": "buy",
             "direction": 1, "volume": 0.1, "llm_action": "approve",
             "llm_comment": "trend supports", "reason": "consensus",
             "executed": 1},
            {"ts": 1794480000.0, "symbol": "EURUSD", "action": "none",
             "direction": 0, "volume": 0.0, "llm_action": "n/a",
             "llm_comment": "", "reason": "no consensus", "executed": 0},
        ]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        return rows[:limit]

    def events(self, limit=100, kind=None, symbol=None):
        rows = [{"ts": 1794490001.0, "kind": "ORDER_FILLED",
                 "symbol": "XAUUSD", "detail": "filled", "retcode": 10009}]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        return rows[:limit]

    def equity(self, limit=500):
        return [{"ts": 1794480000.0 + i * 60, "equity": 100000.0 + i,
                 "balance": 100000.0, "margin": 0.0, "floating": float(i)}
                for i in range(min(limit, 5))]

    def guardian(self):
        return {"day": "2026-11-13", "day_start_equity": 100149.78,
                "halted_day": None, "halt_reason": "", "halted_at": None,
                "updated": 1794490000.0}

    def stats(self):
        return {"decisions": 107, "executed": 7, "filled_orders": 7}


class FakeRunner:
    def __init__(self):
        self.mode = "stopped"
        self.pid = None
        self.started = 0
        self.stopped = 0

    def status(self):
        return {"mode": self.mode, "pid": self.pid,
                "started": None, "stale": False}

    def start(self):
        self.started += 1
        self.mode = "dry-run"
        self.pid = 4242
        return self.status()

    def stop(self):
        self.stopped += 1
        self.mode = "stopped"
        self.pid = None
        return self.status()


class FakeCommittee:
    def snapshot(self):
        return {"available": True, "stale": False, "age_seconds": 12,
                "mode": "LIVE", "symbols": {}}


class FakeMarket:
    def quote(self, symbol):
        return {"symbol": symbol, "bid": 3859.9, "ask": 3860.0,
                "digits": 2, "point": 0.01, "spread_points": 10.0,
                "time": 1794500000.0}

    def overview(self, symbol):
        return {"symbol": symbol, "quote": {"bid": 1.0, "ask": 1.1},
                "kronos": {"available": False, "reason": "no forecast"},
                "session": "london"}


class FakeStrategies:
    def list(self):
        return {"strategies": [{"name": "trend", "enabled": True,
                                "research_only": True}],
                "research_only": True, "live_path": "legacy"}

    def update(self, name, enabled=None, params=None):
        return {"name": name, "enabled": enabled, "params": params or {}}


class FakeLearning:
    def summary(self):
        return {"available": False, "reason": "no closed-trade store"}

    def trades(self, limit=100):
        return {"trades": [], "available": False,
                "reason": "no closed-trade store"}


class FakeBacktests:
    def list(self):
        return {"jobs": [], "artifacts": []}


@pytest.fixture()
def config_dict():
    return {
        "mt5": {"login": 5057915388, "password": "super-secret",
                "server": "MetaQuotes-Demo", "terminal_path": "",
                "timeout_ms": 60000, "deviation_points": 20},
        "trading": {"symbols": ["XAUUSD", "EURUSD"], "symbol": "XAUUSD",
                    "timeframe": "M15", "magic": 260101, "bars_history": 300,
                    "poll_seconds": 15, "close_on_opposite": True,
                    "require_llm_approval": False, "comment": "TradingAgents"},
        "agents": {"ema_fast": 12, "ema_slow": 34, "rsi_period": 14,
                   "rsi_overbought": 70, "rsi_oversold": 30, "atr_period": 14,
                   "atr_sl_mult": 1.5, "atr_tp_mult": 3.0,
                   "weight_trend": 1.1, "weight_momentum": 0.9,
                   "weight_reversion": 0.4, "weight_breakout": 1.0,
                   "weight_pullback": 0.6, "breakout_lookback": 24,
                   "weight_kronos": 1.1, "consensus_threshold": 0.3},
        "kronos": {"enabled": True, "ensemble_runs": 3, "timeframes": ["H1"],
                   "lookback": 384, "pred_len": 16, "min_agreement": 0.6,
                   "max_context": 512},
        "news": {"enabled": True},
        "telegram": {"enabled": False, "bot_token": "telegram-secret",
                     "chat_id": 0},
        "risk": {"risk_percent_per_trade": 0.5, "fixed_lot": 0.0,
                 "max_notional_leverage_per_position": 2.5, "max_positions": 2,
                 "max_open_same_symbol": 1, "max_daily_loss_percent": 3.0,
                 "max_equity_drawdown_percent": 15.0, "max_spread_points": 40,
                 "max_spread_points_by_symbol": {"XAUUSD": 80},
                 "breakeven_atr": 1.0, "trailing_atr": 1.0,
                 "trailing_step_atr": 0.25, "partial_close_enabled": True,
                 "partial_close_at_r": 1.0, "partial_close_pct": 50,
                 "time_stop_minutes": 0, "dead_market_atr_ratio": 0.62,
                 "trading_hours_utc": [0, 24],
                 "min_gap_seconds_between_same_symbol_trades": 300},
        "llm": {"enabled": True, "gemini_model": "gemini-3.5-flash",
                "api_key": "gemini-secret", "api_key_file": "",
                "min_seconds_between_reviews": 20, "timeout_seconds": 40},
        "strategies": {"enabled": True, "engines": {}},
        "ea": {"enabled": False, "host": "127.0.0.1", "port": 8765,
               "token_file": "state/ea_token.txt", "allow_live": False},
        "logging": {"db_file": "state/agents.sqlite",
                    "log_file": "state/agents.log"},
    }


@pytest.fixture()
def services(tmp_path, config_dict):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_dict, indent=2), encoding="utf-8")
    static_dir = tmp_path / "dist"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>test console</title></head>"
        "<body><div id=root></div></body></html>", encoding="utf-8")
    settings = ApiSettings(
        root=tmp_path,
        config_path=config_path,
        db_path=tmp_path / "state" / "dashboard.sqlite",
        token_file=tmp_path / "state" / "dashboard_token.txt",
        dashboard_token="test-token-123",
        bridge_host="127.0.0.1",
        bridge_port=8765,
        log_paths=(tmp_path / "run.log",),
        static_dir=static_dir,
        ws_interval=0.05,
    )
    store = DashboardStore(settings.db_path)
    mt5 = FakeMT5()
    journal = FakeJournal()
    runner = FakeRunner()
    settings_service = SettingsService(settings, store)
    risk = __import__("api.services.risk_service", fromlist=["RiskService"]).RiskService(
        settings, store, mt5, journal, runner, settings_service)
    services = Services(
        settings=settings,
        store=store,
        mt5=mt5,
        journal=journal,
        runner=runner,
        risk=risk,
        config=settings_service,
        market=FakeMarket(),
        strategies=FakeStrategies(),
        committee=FakeCommittee(),
        learning=FakeLearning(),
        backtests=FakeBacktests(),
        bridge_check=lambda: {"listening": True, "detail": "fake bridge"},
    )
    yield services
    store.close()


@pytest.fixture()
def client(services):
    app = create_app(settings=services.settings, services=services)
    with TestClient(app) as test_client:
        yield test_client


AUTH = {"Authorization": "Bearer test-token-123"}
MUTATE = {**AUTH, "X-Dashboard-Request": "1"}


# ------------------------------------------------------------------- health
def test_health_is_public_and_reports_each_check(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in ("ok", "degraded")
    assert set(payload["checks"]) >= {"mt5", "database", "bridge", "runner", "token"}
    assert payload["checks"]["bridge"]["listening"] is True


def test_spa_routes_serve_the_built_index_with_security_headers(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "test console" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_static_files_cannot_escape_the_build_directory(client, tmp_path):
    (tmp_path / "secret.txt").write_text("do-not-serve", encoding="utf-8")
    for path in ("/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "do-not-serve" not in response.text


def test_unknown_asset_is_404_not_the_spa(client):
    response = client.get("/assets/missing.js")
    assert response.status_code == 404


def test_account_requires_a_bearer_token(client):
    response = client.get("/api/v1/account")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_account_returns_the_broker_snapshot(client):
    response = client.get("/api/v1/account", headers=AUTH)
    assert response.status_code == 200
    payload = response.json()
    assert payload["login"] == 5057915388
    assert payload["balance"] == pytest.approx(100149.78)


def test_positions_and_orders_are_wrapped_with_counts(client):
    positions = client.get("/api/v1/positions", headers=AUTH).json()
    orders = client.get("/api/v1/orders", headers=AUTH).json()
    assert positions["count"] == 2
    assert positions["positions"][0]["symbol"] == "XAUUSD"
    assert orders["count"] == 1


# -------------------------------------------------------------------- auth
def test_login_accepts_the_local_token_and_rejects_a_wrong_one(client):
    ok = client.post("/api/v1/auth/login", json={"token": "test-token-123"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = client.post("/api/v1/auth/login", json={"token": "nope"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"


def test_mutation_requires_the_request_header(client):
    response = client.post("/api/v1/runner/start", headers=AUTH, json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_request_header"


# ------------------------------------------------------------------ runner
def test_runner_start_defaults_to_dry_run_and_replays_idempotently(client, services):
    headers = {**MUTATE, "Idempotency-Key": "start-1"}
    first = client.post("/api/v1/runner/start", headers=headers, json={})
    second = client.post("/api/v1/runner/start", headers=headers, json={})
    assert first.status_code == 200, first.text
    assert first.json()["runner"]["mode"] == "dry-run"
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert services.runner.started == 1


def test_runner_refuses_live_mode(client, services):
    response = client.post(
        "/api/v1/runner/start",
        headers={**MUTATE, "Idempotency-Key": "start-live"},
        json={"mode": "live"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "live_disabled"
    assert services.runner.started == 0


# -------------------------------------------------------------------- risk
def test_risk_exposes_limits_and_guardian_state(client):
    payload = client.get("/api/v1/risk", headers=AUTH).json()
    assert payload["limits"]["risk_percent_per_trade"] == 0.5
    assert payload["guardian"]["day"] == "2026-11-13"
    assert payload["open_positions"] == 2


def test_emergency_close_is_dry_run_by_default_and_audits(client, services):
    headers = {**MUTATE, "Idempotency-Key": "close-dry-1"}
    response = client.post("/api/v1/risk/emergency-close", headers=headers, json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["positions"] == 2
    assert payload["closed"] == 0
    assert services.mt5.close_calls == 0
    audit = services.store.recent_audit(5)
    assert any(row["action"] == "risk.emergency_close" for row in audit)


def test_emergency_close_requires_confirmation_for_a_real_close(client, services):
    headers = {**MUTATE, "Idempotency-Key": "close-real-1"}
    bad = client.post("/api/v1/risk/emergency-close", headers=headers,
                      json={"dry_run": False, "confirm": "nope"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "confirmation_required"
    assert services.mt5.close_calls == 0

    good_headers = {**MUTATE, "Idempotency-Key": "close-real-2"}
    good = client.post("/api/v1/risk/emergency-close", headers=good_headers,
                       json={"dry_run": False, "confirm": "CLOSE ALL"})
    assert good.status_code == 200
    assert good.json()["closed"] == 2
    assert services.mt5.close_calls == 1


# ---------------------------------------------------------------- settings
def test_settings_redacts_every_secret(client):
    response = client.get("/api/v1/settings", headers=AUTH)
    assert response.status_code == 200
    text = response.text
    assert "gemini-secret" not in text
    assert "telegram-secret" not in text
    assert "super-secret" not in text
    payload = response.json()
    assert payload["settings"]["llm"]["api_key_present"] is True
    assert payload["settings"]["telegram"]["bot_token_present"] is True
    assert "api_key" not in payload["settings"]["llm"]
    assert "password" not in payload["settings"]["mt5"]


def test_settings_update_is_blocked_while_the_runner_is_active(client, services):
    services.runner.mode = "dry-run"
    response = client.patch(
        "/api/v1/settings", headers=MUTATE,
        json={"risk.risk_percent_per_trade": 1.0},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "runner_active"


def test_settings_update_writes_config_and_keeps_secrets(client, services):
    response = client.patch(
        "/api/v1/settings", headers=MUTATE,
        json={"risk.risk_percent_per_trade": 1.0,
              "trading.timeframe": "M5"},
    )
    assert response.status_code == 200, response.text
    payload = json.loads(services.settings.config_path.read_text(encoding="utf-8"))
    assert payload["risk"]["risk_percent_per_trade"] == 1.0
    assert payload["trading"]["timeframe"] == "M5"
    assert payload["llm"]["api_key"] == "gemini-secret"
    assert list(services.settings.root.glob("config.json.bak-dashboard-*"))


def test_settings_rejects_unknown_keys_and_out_of_range_values(client):
    unknown = client.patch("/api/v1/settings", headers=MUTATE,
                           json={"llm.api_key": "leak"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"
    out_of_range = client.patch(
        "/api/v1/settings", headers=MUTATE,
        json={"risk.risk_percent_per_trade": 99.0})
    assert out_of_range.status_code == 422


# ------------------------------------------------------------------ journal
def test_journal_endpoints_filter_and_wrap(client):
    decisions = client.get("/api/v1/decisions?symbol=XAUUSD&limit=10",
                           headers=AUTH).json()
    assert decisions["count"] == 1
    assert decisions["decisions"][0]["symbol"] == "XAUUSD"
    equity = client.get("/api/v1/equity?limit=5", headers=AUTH).json()
    assert equity["count"] == 5
    events = client.get("/api/v1/events?kind=ORDER_FILLED", headers=AUTH).json()
    assert events["count"] == 1


# -------------------------------------------------------------------- misc
def test_market_and_committee_and_learning_endpoints_exist(client):
    quote = client.get("/api/v1/market/quote?symbol=XAUUSD", headers=AUTH)
    assert quote.status_code == 200
    overview = client.get("/api/v1/market/overview?symbol=XAUUSD", headers=AUTH)
    assert overview.status_code == 200
    committee = client.get("/api/v1/committee", headers=AUTH)
    assert committee.status_code == 200
    learning = client.get("/api/v1/learning/summary", headers=AUTH)
    assert learning.status_code == 200
    strategies = client.get("/api/v1/strategies", headers=AUTH)
    assert strategies.status_code == 200
    assert strategies.json()["research_only"] is True


def test_websocket_requires_auth_then_pushes_a_snapshot(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(Exception):
            ws.receive_json()

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "test-token-123"})
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["account"]["login"] == 5057915388
        assert snapshot["runner"]["mode"] in ("stopped", "dry-run")
