"""Market structure: swings, BOS, sweeps, FVG, order blocks, premium/discount."""
from __future__ import annotations

import numpy as np
import pandas as pd

from market.structure import (
    confirmed_swings,
    fair_value_gaps,
    market_structure,
    order_blocks,
    premium_discount,
)


def _flat(n: int = 30, price: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame({
        "time": np.arange(n) * 900,
        "open": np.full(n, price),
        "high": np.full(n, price + 0.5),
        "low": np.full(n, price - 0.5),
        "close": np.full(n, price),
        "tick_volume": np.full(n, 100.0),
    })


def test_bos_up_after_confirmed_swing_high():
    df = _flat(30)
    df.loc[5, ["open", "high", "low", "close"]] = [14.0, 15.0, 13.5, 14.0]
    df.loc[10, ["open", "high", "low", "close"]] = [14.0, 16.0, 13.8, 15.8]
    out = market_structure(df, lookback=2)
    assert not bool(out.loc[6, "bos_up"])
    assert bool(out.loc[10, "bos_up"])
    assert out.loc[10, "last_swing_high"] == 15.0


def test_liquidity_sweep_high_rejects():
    df = _flat(30)
    df.loc[5, ["open", "high", "low", "close"]] = [14.0, 15.0, 13.5, 14.0]
    df.loc[10, ["open", "high", "low", "close"]] = [14.0, 16.0, 13.8, 14.2]
    out = market_structure(df, lookback=2)
    assert bool(out.loc[10, "sweep_high"])
    assert not bool(out.loc[10, "bos_up"])
    assert out.loc[10, "sweep_high_level"] == 15.0


def test_bullish_fvg_is_tracked_until_filled():
    df = _flat(8)
    df.loc[0, ["open", "high", "low", "close"]] = [99.5, 100.0, 99.0, 99.8]
    df.loc[1, ["open", "high", "low", "close"]] = [99.8, 101.0, 99.5, 100.8]
    df.loc[2, ["open", "high", "low", "close"]] = [104.0, 105.0, 103.0, 104.5]
    df.loc[3, ["open", "high", "low", "close"]] = [104.0, 104.5, 102.0, 103.0]
    df.loc[4, ["open", "high", "low", "close"]] = [103.0, 103.5, 99.0, 100.0]
    out = fair_value_gaps(df)
    assert bool(out.loc[2, "fvg_bull"])
    assert out.loc[2, "fvg_bull_top"] == 103.0
    assert out.loc[2, "fvg_bull_bottom"] == 100.0
    # bar 3 creates a newer, tighter bullish gap (101..102); the most recent
    # active zone is the one a strategy should see
    assert out.loc[3, "fvg_bull_top"] == 102.0
    assert out.loc[3, "fvg_bull_bottom"] == 101.0
    assert np.isnan(out.loc[4, "fvg_bull_top"])


def test_bullish_order_block_before_displacement():
    df = _flat(20)
    df.loc[0, ["open", "high", "low", "close"]] = [10.0, 10.5, 8.5, 9.0]
    df.loc[1, ["open", "high", "low", "close"]] = [9.0, 12.5, 8.8, 12.0]
    out = order_blocks(df, displacement_atr=1.0)
    assert bool(out.loc[1, "ob_bull"])
    assert out.loc[1, "ob_bull_top"] == 10.5
    assert out.loc[1, "ob_bull_bottom"] == 8.5


def test_premium_discount_position():
    df = _flat(30)
    df.loc[5, ["open", "high", "low", "close"]] = [15.0, 20.0, 9.0, 15.0]
    df.loc[7, ["open", "high", "low", "close"]] = [14.5, 15.0, 14.0, 14.5]
    swings = confirmed_swings(df, lookback=2)
    out = premium_discount(swings)
    assert out.loc[7, "range_high"] == 20.0
    assert out.loc[7, "range_low"] == 9.0
    assert abs(float(out.loc[7, "range_position"]) - 0.5) < 1e-9


def test_swing_lookback_delays_confirmation():
    df = _flat(20)
    df.loc[5, "high"] = 20.0
    out = confirmed_swings(df, lookback=2)
    assert not bool(out.loc[5, "swing_high_confirmed"])
    assert bool(out.loc[7, "swing_high_confirmed"])
    assert out.loc[7, "swing_high_level"] == 20.0
