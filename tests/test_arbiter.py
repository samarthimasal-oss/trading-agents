"""Arbitration rules: ranking, thresholds, duplicates and direction caps."""
from __future__ import annotations

from strategies.arbiter import StrategyArbiter
from strategies.base import StrategySignal


def _signal(strategy="trend", direction=1, confidence=0.70, rr=2.0,
            setup_id=None, expires_at=10_000.0, bar_time=0.0) -> StrategySignal:
    risk = 1.0
    entry = 100.0
    stop = entry - direction * risk
    target = entry + direction * rr * risk
    return StrategySignal(
        strategy=strategy,
        symbol="XAUUSD",
        timeframe="M15",
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        rr=rr,
        confidence=confidence,
        expires_at=expires_at,
        setup_id=setup_id or f"{strategy}-{direction}-{confidence}-{rr}",
        bar_time=bar_time,
    )


def _config(**overrides) -> dict:
    section = {
        "enabled": True,
        "min_confidence": 0.50,
        "min_rr": 0.80,
        "max_signals_per_bar": 2,
        "allow_opposite_directions": False,
        "weights": {"trend": 1.0, "breakout": 1.0, "smc": 1.0,
                    "momentum": 1.0, "mean_reversion": 1.0},
    }
    section.update(overrides)
    return {"strategies": section}


def test_good_signal_is_selected():
    arbiter = StrategyArbiter(_config())
    result = arbiter.arbitrate([_signal()], now=1.0)
    assert len(result.selected) == 1
    assert not result.rejected


def test_low_confidence_is_rejected():
    arbiter = StrategyArbiter(_config(min_confidence=0.80))
    result = arbiter.arbitrate([_signal(confidence=0.60)], now=1.0)
    assert not result.selected
    assert "confidence" in result.rejected[0].reason


def test_expired_signal_is_rejected():
    arbiter = StrategyArbiter(_config())
    result = arbiter.arbitrate([_signal(expires_at=5.0)], now=10.0)
    assert not result.selected
    assert result.rejected[0].reason == "expired"


def test_low_rr_is_rejected():
    arbiter = StrategyArbiter(_config(min_rr=1.5))
    result = arbiter.arbitrate([_signal(rr=1.0)], now=1.0)
    assert not result.selected
    assert "rr" in result.rejected[0].reason


def test_one_signal_per_strategy():
    arbiter = StrategyArbiter(_config())
    result = arbiter.arbitrate([
        _signal(setup_id="trend-a", confidence=0.9),
        _signal(setup_id="trend-b", confidence=0.8),
    ], now=1.0)
    assert len(result.selected) == 1
    assert result.selected[0].setup_id == "trend-a"
    assert any(r.reason == "strategy already proposed" for r in result.rejected)


def test_duplicate_setup_id_across_strategies():
    arbiter = StrategyArbiter(_config(max_signals_per_bar=3))
    result = arbiter.arbitrate([
        _signal(strategy="trend", setup_id="same", confidence=0.9),
        _signal(strategy="breakout", setup_id="same", confidence=0.8),
    ], now=1.0)
    assert len(result.selected) == 1
    assert any(r.reason == "duplicate setup id" for r in result.rejected)


def test_opposite_directions_keep_top_ranked():
    arbiter = StrategyArbiter(_config())
    result = arbiter.arbitrate([
        _signal(strategy="trend", direction=1, confidence=0.9, setup_id="long"),
        _signal(strategy="breakout", direction=-1, confidence=0.7, setup_id="short"),
    ], now=1.0)
    assert [s.setup_id for s in result.selected] == ["long"]
    assert any("opposite direction" in r.reason for r in result.rejected)


def test_allow_opposite_directions_selects_both():
    arbiter = StrategyArbiter(_config(allow_opposite_directions=True))
    result = arbiter.arbitrate([
        _signal(strategy="trend", direction=1, confidence=0.9, setup_id="long"),
        _signal(strategy="breakout", direction=-1, confidence=0.7, setup_id="short"),
    ], now=1.0)
    assert len(result.selected) == 2


def test_per_bar_cap_keeps_highest_score():
    arbiter = StrategyArbiter(_config(max_signals_per_bar=1))
    result = arbiter.arbitrate([
        _signal(strategy="trend", confidence=0.6, setup_id="low"),
        _signal(strategy="breakout", confidence=0.9, setup_id="high"),
    ], now=1.0)
    assert [s.setup_id for s in result.selected] == ["high"]
    assert any(r.reason == "per-bar signal cap" for r in result.rejected)


def test_weights_influence_ranking():
    arbiter = StrategyArbiter(_config(
        max_signals_per_bar=1,
        weights={"trend": 0.5, "breakout": 2.0, "smc": 1.0,
                 "momentum": 1.0, "mean_reversion": 1.0},
    ))
    result = arbiter.arbitrate([
        _signal(strategy="trend", confidence=0.9, setup_id="trend"),
        _signal(strategy="breakout", confidence=0.7, setup_id="breakout"),
    ], now=1.0)
    assert [s.setup_id for s in result.selected] == ["breakout"]


def test_master_switch_off_rejects_everything():
    arbiter = StrategyArbiter(_config(enabled=False))
    result = arbiter.arbitrate([_signal()], now=1.0)
    assert not result.selected
    assert "master switch" in result.rejected[0].reason
