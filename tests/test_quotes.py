"""Quote quarantine must reject every unusable feed shape."""
from __future__ import annotations

from core.quotes import assess_quote, spread_limit_for


def test_valid_quote_passes():
    q = assess_quote(4012.79, 4013.04, 0.01, max_spread_points=40)
    assert q.valid
    assert round(q.spread_points, 1) == 25.0


def test_zero_bid_is_quarantined():
    q = assess_quote(0.0, 1.3317, 0.00001, max_spread_points=40)
    assert not q.valid
    assert "non-positive" in q.reason


def test_crossed_quote_is_quarantined():
    q = assess_quote(1.1002, 1.1000, 0.00001)
    assert not q.valid
    assert "crossed" in q.reason


def test_negative_spread_is_quarantined():
    q = assess_quote(1.1000, 1.0999, 0.00001)
    assert not q.valid


def test_wide_spread_is_quarantined():
    q = assess_quote(1.1000, 1.1005, 0.00001, max_spread_points=40)
    assert not q.valid
    assert "spread" in q.reason


def test_stale_tick_is_quarantined():
    q = assess_quote(1.1000, 1.1001, 0.00001, tick_time=1000.0,
                     now=1010.0, max_age_seconds=5)
    assert not q.valid
    assert "stale" in q.reason


def test_fresh_tick_passes():
    q = assess_quote(1.1000, 1.1001, 0.00001, tick_time=1000.0,
                     now=1002.0, max_age_seconds=5)
    assert q.valid


def test_per_symbol_spread_limit_overrides_global():
    risk = {"max_spread_points": 40,
            "max_spread_points_by_symbol": {"XAUUSD": 80, "EURUSD": 20}}
    assert spread_limit_for("XAUUSD", risk) == 80
    assert spread_limit_for("XAUUSD.a", risk) == 80
    assert spread_limit_for("EURUSD", risk) == 20
    assert spread_limit_for("USDJPY", risk) == 40


def test_per_symbol_spread_limit_defaults_without_map():
    assert spread_limit_for("XAUUSD", {"max_spread_points": 40}) == 40
