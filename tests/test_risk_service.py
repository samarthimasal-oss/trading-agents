"""Risk snapshot maths against fakes (no MT5, no journal file)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api.services.risk_service import RiskService


class FakeConfig:
    def __init__(self, config: dict):
        self.config = config

    def raw(self) -> dict:
        return self.config


class FakeMT5:
    def __init__(self, equity: float, daily_profit: float = 0.0):
        self.equity = equity
        self.daily_profit = daily_profit
        self.close_calls = 0

    def account(self):
        return {"login": 1, "balance": self.equity, "equity": self.equity,
                "currency": "USD", "available": True}

    def positions(self, symbol=None, magic=None):
        return []

    def daily_bot_profit(self):
        return self.daily_profit

    def close_all(self, magic=None):
        self.close_calls += 1
        return 0


class FakeJournal:
    def __init__(self, guardian: dict, equity_rows: list[dict]):
        self.guardian_state = guardian
        self.equity_rows = equity_rows

    def guardian(self) -> dict:
        return self.guardian_state

    def equity(self, limit: int = 500) -> list[dict]:
        return self.equity_rows


class FakeRunner:
    def status(self) -> dict:
        return {"mode": "stopped", "pid": None, "started": None}


def build(guardian: dict | None = None, equity_rows: list[dict] | None = None,
          equity: float = 100_150.0, daily_profit: float = 0.0) -> RiskService:
    config = FakeConfig({
        "trading": {"magic": 770001},
        "risk": {"max_daily_loss_percent": 3.0, "max_positions": 2},
    })
    return RiskService(
        settings=SimpleNamespace(),
        store=SimpleNamespace(),
        mt5=FakeMT5(equity, daily_profit),
        journal=FakeJournal(guardian or {"available": False}, equity_rows or []),
        runner=FakeRunner(),
        config=config,
    )


def test_daily_pnl_prefers_the_guardian_day_start():
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    service = build(
        guardian={"available": True, "day": day, "day_start_equity": 100_000.0},
        equity_rows=[{"ts": 1, "equity": 100_200.0}, {"ts": 2, "equity": 100_100.0}],
        equity=100_150.0,
    )

    snapshot = service.snapshot()

    assert snapshot["day"] == day
    assert snapshot["daily_pnl"] == pytest.approx(150.0)
    assert snapshot["daily_pnl_source"] == "guardian day start"
    assert snapshot["daily_loss_limit_usd"] == pytest.approx(3004.5)
    assert snapshot["peak_equity"] == pytest.approx(100_200.0)
    assert snapshot["peak_equity_ts"] == 1
    assert snapshot["drawdown_from_peak_pct"] == pytest.approx(0.0499, abs=0.001)


def test_daily_pnl_falls_back_to_mt5_deals_without_guardian_state():
    service = build(guardian={"available": False}, daily_profit=-42.5)

    snapshot = service.snapshot()

    assert snapshot["daily_pnl"] == pytest.approx(-42.5)
    assert snapshot["daily_pnl_source"] == "MT5 deals today + floating"


def test_peak_equity_uses_current_equity_when_journal_is_empty():
    service = build(guardian={"available": False}, equity_rows=[], equity=100_150.0)

    snapshot = service.snapshot()

    assert snapshot["peak_equity"] == pytest.approx(100_150.0)
    assert snapshot["peak_equity_ts"] is None
    assert snapshot["drawdown_from_peak_pct"] == pytest.approx(0.0)
