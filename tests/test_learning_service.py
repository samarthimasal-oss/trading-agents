"""Learning Centre service: closed trades derived from MT5 deal history."""
from __future__ import annotations

import pytest

from api.errors import APIError
from api.services.learning_service import LearningService


class FakeHistoryMT5:
    def __init__(self, rows, fail=False, no_settings=(), default_magic=None):
        self.rows = rows
        self.fail = fail
        self.no_settings = set(no_settings)
        self.default_magic = default_magic

    def closed_positions(self, days=180, magic=None):
        if self.fail:
            raise APIError(503, "mt5_unavailable", "terminal offline")
        magic = self.default_magic if magic is None else magic
        return [row for row in self.rows
                if magic is None or row.get("magic") == magic]

    def symbol_settings(self, symbol):
        if symbol in self.no_settings:
            raise APIError(503, "mt5_unavailable", "symbol settings unavailable")
        return {"symbol": symbol, "trade_tick_value": 1.0,
                "trade_tick_size": 0.01, "trade_contract_size": 100.0,
                "point": 0.01, "digits": 2}


class FakeJournal:
    available = True


def _row(**overrides):
    base = {
        "position_id": 901,
        "symbol": "XAUUSD",
        "side": "buy",
        "volume": 0.1,
        "entry_price": 3850.0,
        "exit_price": 3860.0,
        "net": 50.0,
        "open_time": 1794400000.0,
        "close_time": 1794407200.0,
        "magic": 770001,
        "comment": "TradingAgents",
        "sl": 3840.0,
        "tp": 3880.0,
    }
    base.update(overrides)
    return base


def test_summary_derives_metrics_and_realized_r():
    mt5 = FakeHistoryMT5([
        _row(),                                  # net +50, SL 10 away -> +0.5R
        _row(position_id=902, side="sell", net=-30.0,
             entry_price=3850.0, exit_price=3853.0, sl=3860.0),
        _row(position_id=903, net=20.0, magic=123456),  # non-bot deal: adapter drops it
    ], default_magic=770001)
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    payload = service.summary()
    assert payload["available"] is True
    assert payload["data_source"].startswith("MT5 deal history")
    metrics = payload["metrics"]
    assert metrics["closed_trades"] == 2
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1
    assert metrics["win_rate"] == pytest.approx(50.0)
    assert metrics["total_net"] == pytest.approx(20.0)
    # risk = |3850 - 3840| / 0.01 * 1.0 * 0.1 = 100 -> +0.5R; -30/100 -> -0.3R
    assert metrics["r_known"] == 2
    assert metrics["avg_r"] == pytest.approx((0.5 - 0.3) / 2)
    assert metrics["bot_trades"] == 2
    assert "sessions" in metrics and "symbols" in metrics
    for field in ("strategy", "mfe", "mae", "exit_reason"):
        assert field in payload["missing_fields"]


def test_trades_rows_expose_derived_fields_and_honest_gaps():
    mt5 = FakeHistoryMT5([_row()])
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    payload = service.trades(limit=10)
    assert payload["available"] is True
    assert payload["count"] == 1
    trade = payload["trades"][0]
    assert trade["is_bot"] is True
    assert trade["session"] in ("tokyo", "london", "london/new-york overlap",
                                "new-york", "late-session")
    assert trade["realized_r"] == pytest.approx(0.5)
    assert trade["risk_basis"] == "entry-order SL"
    # fields the deal record cannot provide
    for field in ("strategy", "mfe", "mae", "exit_reason", "committee_reasoning"):
        assert field not in trade


def test_r_is_none_without_entry_sl():
    row = _row(position_id=904)
    row.pop("sl")
    mt5 = FakeHistoryMT5([row])
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    payload = service.trades(limit=10)
    trade = payload["trades"][0]
    assert trade["realized_r"] is None
    assert trade["risk_basis"] is None
    summary = service.summary()
    assert summary["metrics"]["r_known"] == 0
    assert summary["metrics"]["avg_r"] is None


def test_symbol_settings_failure_degrades_to_null_r():
    mt5 = FakeHistoryMT5([_row()], no_settings={"XAUUSD"})
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    payload = service.summary()
    assert payload["available"] is True
    assert payload["metrics"]["r_known"] == 0


def test_unavailable_when_mt5_history_fails():
    mt5 = FakeHistoryMT5([], fail=True)
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    summary = service.summary()
    assert summary["available"] is False
    assert "unavailable" in summary["reason"]
    trades = service.trades(limit=5)
    assert trades["available"] is False
    assert trades["trades"] == []


def test_zero_trades_is_available_but_empty():
    mt5 = FakeHistoryMT5([])
    service = LearningService("/tmp", FakeJournal(), mt5=mt5, magic=770001)
    summary = service.summary()
    assert summary["available"] is True
    assert summary["closed_trades"] == 0
    assert "no closed trades" in summary["reason"]
    trades = service.trades(limit=5)
    assert trades["available"] is True
    assert trades["count"] == 0
    assert "no closed trades" in trades["reason"]
