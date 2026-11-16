"""Market snapshot: timeframe-aware candles, Kronos honesty and news blocking."""
from __future__ import annotations

import json

import pytest

from api.services.market_service import MarketService

NOW = 1794672000.0


class MarketStub:
    def __init__(self, bars: int = 60):
        self.bars = bars
        self.calls: list[tuple[str, str, int]] = []

    def candles(self, symbol: str, timeframe: str, count: int):
        self.calls.append((symbol, timeframe, count))
        rows = []
        for index in range(min(count, self.bars)):
            time = NOW - (min(count, self.bars) - 1 - index) * 3600
            base = 3900.0 + index * 0.7
            rows.append({
                "time": float(time),
                "open": base - 0.3,
                "high": base + 1.1,
                "low": base - 1.2,
                "close": base + 0.4,
                "tick_volume": 100.0 + index,
            })
        return rows

    def quote(self, symbol: str):
        return {"symbol": symbol, "requested_symbol": symbol,
                "bid": 3937.8, "ask": 3937.9, "digits": 2, "point": 0.01,
                "spread_points": 10.0, "time": NOW}


@pytest.fixture()
def service(tmp_path):
    stub = MarketStub()
    cfg = {
        "risk": {"max_spread_points": 30.0},
        "news": {"enabled": True, "window_before_min": 10, "window_after_min": 10,
                 "min_impact": "High"},
    }
    return MarketService(stub, tmp_path, config_loader=lambda: cfg,
                         clock=lambda: NOW), stub, tmp_path


def test_overview_uses_the_requested_timeframe_for_candles_and_features(service):
    svc, stub, _ = service
    payload = svc.overview("XAUUSD", timeframe="H1")
    assert payload["timeframe"] == "H1"
    assert stub.calls[-1] == ("XAUUSD", "H1", 240)
    assert len(payload["candles"]) == 60
    assert payload["features"]["available"] is True
    assert payload["kronos"]["available"] is False
    assert "no Kronos cache" in payload["kronos"]["reason"]
    assert payload["tradable"] is True
    assert payload["spread"]["points"] == 10.0


def test_stale_kronos_exposes_base_and_summary_and_blocks_trading(service):
    svc, _, tmp_path = service
    state = tmp_path / "state"
    state.mkdir()
    (state / "kronos_tf_cache.json").write_text(json.dumps({
        "XAUUSD|H1": {
            "last_ts": NOW - 86400,
            "payload": {
                "dir": 1, "move": 5.76, "move_next": 0.95,
                "path_hi": 8.91, "path_lo": -4.97,
                "base_close": 4083.36, "base_ts": NOW - 86400,
                "summary": "Kronos sees price UP 0.14% over ~16.0h",
                "candles": [[NOW, 4000, 4010, 3990, 4005]],
            },
        },
    }), encoding="utf-8")

    payload = svc.overview("XAUUSD", timeframe="H1")
    kronos = payload["kronos"]
    assert kronos["available"] is True
    assert kronos["stale"] is True
    assert kronos["base_close"] == 4083.36
    assert kronos["summary"].startswith("Kronos sees price UP")
    assert kronos["direction"] == 1
    assert "forecast is stale" in payload["reasons"]
    assert payload["tradable"] is False


def test_high_impact_news_window_blocks_and_lists_upcoming_events(service):
    svc, _, tmp_path = service
    state = tmp_path / "state"
    state.mkdir()
    (state / "news_calendar.json").write_text(json.dumps({
        "ts": NOW,
        "events": [
            {"ts": NOW - 60, "title": "Non-farm payrolls", "currency": "USD",
             "impact": "High"},
            {"ts": NOW + 3600, "title": "CPI", "currency": "USD", "impact": "High"},
        ],
    }), encoding="utf-8")

    payload = svc.overview("XAUUSD")
    news = payload["news"]
    assert news["available"] is True
    assert news["blocked"] is True
    assert news["events"][0]["title"] == "Non-farm payrolls"
    assert news["events"][0]["blocking"] is True
    assert [event["title"] for event in news["upcoming"]] == ["CPI"]
    assert "high-impact news window" in payload["reasons"]
