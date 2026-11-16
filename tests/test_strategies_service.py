"""Strategy Desk service: closed-bar scans, overrides and replay honesty."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from api.errors import APIError
from api.services.strategies_service import StrategiesService

END = 1794671100.0  # 2026-11-14 15:45 UTC, a London/NY overlap bar


class ConfigStub:
    def __init__(self, data: dict):
        self.data = data

    def raw(self) -> dict:
        return self.data


class StoreStub:
    def __init__(self):
        self.audits = []

    def audit(self, *args, **kwargs):
        self.audits.append((args, kwargs))


class MarketStub:
    def __init__(self, bars: int = 420, spread_points: float = 5.0,
                 quote: bool = True):
        self.bars = bars
        self.spread_points = spread_points
        self.quote_available = quote

    def candles(self, symbol: str, timeframe: str, bars: int):
        out = []
        for index in range(min(bars, self.bars)):
            time = END - (bars - 1 - index) * 900
            base = 3800.0 + index * 0.35 + math.sin(index / 11.0) * 1.8
            out.append({
                "time": float(time),
                "open": base - 0.2,
                "high": base + 1.8,
                "low": base - 1.6,
                "close": base + 0.3,
                "tick_volume": 100.0 + index % 17,
            })
        return out

    def quote(self, symbol: str):
        if not self.quote_available:
            raise RuntimeError("terminal is offline")
        return {"symbol": symbol, "bid": 4300.0, "ask": 4300.0 + self.spread_points * 0.01,
                "point": 0.01, "digits": 2, "spread_points": self.spread_points}


@pytest.fixture()
def service(tmp_path):
    cfg = {
        "trading": {"symbol": "XAUUSD", "timeframe": "M15"},
        "strategies": {"enabled": True, "engines": {}},
        "market": {},
        "risk": {},
    }
    store = StoreStub()
    svc = StrategiesService(tmp_path, ConfigStub(cfg), store,
                            mt5=MarketStub(), clock=lambda: END + 60)
    return svc, store, tmp_path


def test_signals_use_closed_bars_and_explain_proposals(service):
    svc, _, _ = service
    result = svc.signals(bars=400)
    assert result["available"] is True
    assert result["closed_bars_only"] is True
    assert result["bars"] == 399  # 400 requested, forming bar discarded
    assert result["forming_bar_time"] == END
    assert result["last_closed_bar"] == END - 900
    assert result["research_only"] is True
    assert len(result["engines"]) == 5
    assert result["proposals"], "the deterministic uptrend should produce a proposal"
    trend = next(row for row in result["proposals"] if row["strategy"] == "trend")
    assert trend["direction"] == 1
    assert trend["status"] in ("selected", "rejected")
    assert trend["score"] > 0


def test_research_override_disables_an_engine(service):
    svc, _, tmp_path = service
    state = tmp_path / "state"
    state.mkdir()
    (state / "strategy_overrides.json").write_text(
        json.dumps({"trend": {"enabled": False}}), encoding="utf-8")
    result = svc.signals(bars=400)
    trend = next(row for row in result["engines"] if row["name"] == "trend")
    assert trend["enabled"] is False
    assert not [row for row in result["proposals"] if row["strategy"] == "trend"]
    assert "overrides" in result["config_source"]


def test_missing_quote_is_reported_not_invented(tmp_path):
    cfg = {"trading": {"symbol": "XAUUSD", "timeframe": "M15"},
           "strategies": {"enabled": True, "engines": {}}, "market": {}}
    svc = StrategiesService(tmp_path, ConfigStub(cfg), StoreStub(),
                            mt5=MarketStub(quote=False), clock=lambda: END + 60)
    result = svc.signals(bars=400)
    assert result["spread_points"] is None
    assert result["spread_assumed_zero"] is True
    assert "offline" in result["spread_source"]


def test_replay_is_json_safe_and_never_claims_validation(service):
    svc, store, _ = service
    result = svc.replay(bars=700)
    assert result["available"] is True
    assert result["closed_bars_only"] is True
    assert result["validation"]["walkforward_passed"] is False
    assert result["trades_total"] == len(result["trades"])
    assert set(result["combined"]) >= {"trades", "total_r", "expectancy_r", "profit_factor"}
    assert len(result["equity_curve"]) >= 1
    # allow_nan=False is the FastAPI serializer contract.
    json.dumps(result, allow_nan=False)
    assert store.audits and store.audits[-1][0][1] == "strategies.replay"


def test_insufficient_history_is_a_structured_422(service):
    svc, _, _ = service
    svc.mt5 = MarketStub(bars=200)
    with pytest.raises(APIError) as exc:
        svc.signals(bars=400)
    assert exc.value.status_code == 422
    assert exc.value.code == "insufficient_data"
