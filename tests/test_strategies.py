"""Contract tests for the five independent strategy engines."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.base import StrategyContext
from strategies.breakout import BreakoutStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy
from strategies.smc import SmcStrategy
from strategies.trend import TrendStrategy


DEFAULTS = {
    "open": 100.0,
    "high": 101.0,
    "low": 99.0,
    "close": 100.0,
    "tick_volume": 100.0,
    "atr": 1.0,
    "session": "london",
    "ema20": 100.0,
    "ema50": 99.0,
    "ema200": 98.0,
    "adx": 30.0,
    "roc_3": 0.0,
    "roc_5": 0.0,
    "roc_10": 0.0,
    "ema20_slope": 0.0,
    "rsi": 55.0,
    "rsi_slope": 0.0,
    "macd_hist": 0.0,
    "macd_hist_slope": 0.0,
    "vol_z": 0.0,
    "body_atr": 0.5,
    "atr_percentile": 0.5,
    "bb_mid": 100.0,
    "bb_upper": 101.0,
    "bb_lower": 99.0,
    "bb_position": 0.5,
    "bb_width_percentile": 0.5,
    "asia_high": np.nan,
    "asia_low": np.nan,
    "london_high": np.nan,
    "london_low": np.nan,
    "pd_high": np.nan,
    "pd_low": np.nan,
    "sweep_high": False,
    "sweep_low": False,
    "sweep_high_level": np.nan,
    "sweep_low_level": np.nan,
    "displacement_up": False,
    "displacement_down": False,
    "bos_up": False,
    "bos_down": False,
    "choch_up": False,
    "choch_down": False,
    "range_position": 0.5,
    "range_mid": 100.0,
    "ob_bull_top": np.nan,
    "ob_bull_bottom": np.nan,
    "ob_bear_top": np.nan,
    "ob_bear_bottom": np.nan,
    "fvg_bull_top": np.nan,
    "fvg_bull_bottom": np.nan,
    "fvg_bear_top": np.nan,
    "fvg_bear_bottom": np.nan,
}


def _context(overrides: dict | None = None, *, prev_close: float | None = None,
             n: int = 220, session: str = "london") -> StrategyContext:
    rows = []
    for i in range(n):
        row = dict(DEFAULTS)
        row["time"] = 1_700_000_000 + i * 900
        row["session"] = session
        rows.append(row)
    frame = pd.DataFrame(rows)
    if prev_close is not None:
        frame.loc[n - 2, "close"] = prev_close
    for key, value in (overrides or {}).items():
        frame.loc[n - 1, key] = value
    return StrategyContext(symbol="XAUUSD", timeframe="M15", frame=frame,
                           point=0.01, digits=2, index=n - 1)


def test_trend_long_signal_has_valid_rr_and_stop():
    ctx = _context({
        "ema20": 101.0, "ema50": 100.0, "ema200": 99.0,
        "ema20_slope": 1.0, "roc_10": 0.02, "adx": 32.0,
        "close": 101.5, "high": 102.0, "low": 100.5,
    })
    signals = TrendStrategy().evaluate(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == 1
    assert sig.stop < sig.entry < sig.target
    assert sig.rr >= 1.5
    assert "EMA20" in sig.evidence[0]


def test_trend_short_signal_mirrors():
    ctx = _context({
        "ema20": 99.0, "ema50": 100.0, "ema200": 101.0,
        "ema20_slope": -1.0, "roc_10": -0.02, "adx": 32.0,
        "close": 98.5, "high": 99.5, "low": 98.0,
    })
    signals = TrendStrategy().evaluate(ctx)
    assert len(signals) == 1
    assert signals[0].direction == -1
    assert signals[0].target < signals[0].entry < signals[0].stop


def test_breakout_long_signal_from_asia_range():
    ctx = _context({
        "asia_high": 101.0, "asia_low": 99.0,
        "close": 101.5, "high": 101.8, "low": 101.0,
        "atr_percentile": 0.3, "vol_z": 1.2,
    }, prev_close=100.8)
    signals = BreakoutStrategy().evaluate(ctx)
    assert len(signals) == 1
    assert signals[0].direction == 1
    assert "Asia range" in " ".join(signals[0].evidence)


def test_smc_long_signal_after_sweep_and_displacement():
    ctx = _context({
        "sweep_low": True, "sweep_low_level": 98.0,
        "displacement_up": True, "range_position": 0.2,
        "ob_bull_top": 101.0, "ob_bull_bottom": 99.0,
        "close": 100.0, "high": 100.5, "low": 99.5,
    })
    signals = SmcStrategy().evaluate(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == 1
    assert sig.stop < 99.0
    assert sig.rr >= 1.5


def test_momentum_long_signal():
    ctx = _context({
        "ema20": 100.0, "ema50": 99.0,
        "close": 101.0, "open": 100.2, "high": 101.2, "low": 100.0,
        "rsi": 60.0, "adx": 25.0, "roc_5": 0.01,
        "macd_hist": 0.5, "macd_hist_slope": 0.1,
        "vol_z": 1.0, "body_atr": 0.8,
    })
    signals = MomentumStrategy().evaluate(ctx)
    assert len(signals) == 1
    assert signals[0].direction == 1
    assert signals[0].rr >= 1.5


def test_mean_reversion_long_signal_targets_mid_band():
    ctx = _context({
        "adx": 20.0, "bb_width_percentile": 0.4,
        "bb_position": 0.1, "rsi": 35.0, "rsi_slope": 1.0,
        "bb_mid": 101.5, "ema200": 100.0,
        "close": 99.0, "open": 98.5, "high": 99.2, "low": 98.0,
    }, prev_close=98.8)
    signals = MeanReversionStrategy().evaluate(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == 1
    assert sig.target == pytest.approx(101.5, abs=1e-6)
    assert sig.rr >= 0.8


def test_disabled_engine_never_signals():
    ctx = _context({
        "ema20": 101.0, "ema50": 100.0, "ema200": 99.0,
        "ema20_slope": 1.0, "roc_10": 0.02, "adx": 32.0,
        "close": 101.5, "high": 102.0, "low": 100.5,
    })
    assert TrendStrategy({"enabled": False}).evaluate(ctx) == []


def test_session_filter_blocks_asia():
    ctx = _context({
        "ema20": 101.0, "ema50": 100.0, "ema200": 99.0,
        "ema20_slope": 1.0, "roc_10": 0.02, "adx": 32.0,
        "close": 101.5, "high": 102.0, "low": 100.5,
    }, session="asia")
    assert TrendStrategy().evaluate(ctx) == []


def test_make_signal_rejects_inverted_stop_and_low_rr():
    ctx = _context()
    assert ctx.make_signal("trend", 1, 100.0, 101.0, 105.0, 0.7) is None
    assert ctx.make_signal("trend", 1, 100.0, 99.0, 101.0, 0.7,
                           min_rr=1.5) is None
    sig = ctx.make_signal("trend", 1, 100.0, 99.0, 102.0, 0.7,
                          min_rr=1.5)
    assert sig is not None and sig.rr == 2.0


def test_signal_setup_id_is_deterministic():
    ctx = _context()
    a = ctx.make_signal("trend", 1, 100.0, 99.0, 102.0, 0.7)
    b = ctx.make_signal("trend", 1, 100.0, 99.0, 102.0, 0.7)
    assert a.setup_id == b.setup_id
    other = _context({"close": 100.5})
    c = other.make_signal("trend", 1, 100.5, 99.0, 102.5, 0.7)
    assert c.setup_id != a.setup_id


def test_registry_builds_all_five_and_respects_master_switch():
    from strategies.registry import STRATEGY_CLASSES, build_strategies

    engines = build_strategies(None)
    assert set(engines) == set(STRATEGY_CLASSES)
    assert all(engine.enabled for engine in engines.values())
    disabled = build_strategies({"strategies": {"enabled": False}})
    assert not any(engine.enabled for engine in disabled.values())
