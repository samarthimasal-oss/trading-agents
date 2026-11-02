"""Causality and correctness tests for the feature layer."""
from __future__ import annotations

import numpy as np
import pandas as pd

from market.features import add_sessions, build_features
from market.structure import confirmed_swings


def _bars(n: int = 320, step: int = 900) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    price = 2000.0
    rows = []
    for i in range(n):
        drift = 0.15 * np.sin(i / 17.0)
        shock = rng.normal(0.0, 1.2)
        open_ = price
        close = max(1.0, price + drift + shock)
        high = max(open_, close) + abs(rng.normal(1.0, 0.4))
        low = min(open_, close) - abs(rng.normal(1.0, 0.4))
        rows.append({
            "time": 1_700_000_000 + i * step,
            "open": open_, "high": high, "low": low, "close": close,
            "tick_volume": float(100 + (i % 23)),
            "spread": 0.2,
        })
        price = close
    return pd.DataFrame(rows)


def test_future_bars_do_not_change_past_features():
    df = _bars(320)
    base = build_features(df)
    cut = 180
    mutated = df.copy()
    mutated.loc[cut + 1:, ["open", "high", "low", "close"]] *= 1.7
    mutated.loc[cut + 1:, "tick_volume"] *= 3.0
    changed = build_features(mutated)

    numeric_cols = [
        c for c in base.columns
        if c not in {"session", "dt_utc"} and pd.api.types.is_numeric_dtype(base[c])
    ]
    for col in numeric_cols:
        left = base.loc[:cut, col].to_numpy(dtype=float)
        right = changed.loc[:cut, col].to_numpy(dtype=float)
        assert np.allclose(left, right, equal_nan=True), f"lookahead in {col}"
    assert base.loc[:cut, "session"].tolist() == changed.loc[:cut, "session"].tolist()


def test_swing_is_confirmed_only_after_lookback():
    df = _bars(40)
    df["high"] = 9.0
    df["low"] = 1.0
    df["open"] = 5.0
    df["close"] = 5.0
    df.loc[10, "high"] = 10.0
    swings = confirmed_swings(df, lookback=2)
    assert not bool(swings.loc[10, "swing_high_confirmed"])
    assert bool(swings.loc[12, "swing_high_confirmed"])
    assert swings.loc[12, "swing_high_level"] == 10.0
    assert swings.loc[13, "last_swing_high"] == 10.0


def test_previous_day_levels_use_only_completed_days():
    rows = []
    for i in range(10):
        rows.append({"time": i * 3600, "open": 100, "high": 100 + i,
                     "low": 90 - i, "close": 99, "tick_volume": 1})
    for i in range(10):
        rows.append({"time": 86_400 + i * 3600, "open": 100, "high": 200 + i,
                     "low": 190 - i, "close": 199, "tick_volume": 1})
    out = build_features(pd.DataFrame(rows))
    first_day = out[out["day_id"] == 0]
    second_day = out[out["day_id"] == 1]
    assert first_day["pd_high"].isna().all()
    assert second_day["pd_high"].iloc[0] == 109.0
    assert second_day["pd_low"].iloc[0] == 81.0
    assert second_day["pd_mid"].iloc[0] == 95.0


def test_session_labels():
    rows = []
    for hour in (0, 6, 7, 11, 12, 15, 16, 20, 21, 23):
        rows.append({"time": 86_400 + hour * 3600, "open": 1, "high": 2,
                     "low": 0.5, "close": 1.5, "tick_volume": 1})
    out = add_sessions(pd.DataFrame(rows))
    assert out["session"].tolist() == [
        "asia", "asia", "london", "london", "overlap", "overlap",
        "newyork", "newyork", "off", "off",
    ]
    assert out.loc[out["session"].eq("london"), "is_trade_session"].all()
    assert not out.loc[out["session"].eq("asia"), "is_trade_session"].any()


def test_features_are_finite_where_expected_after_warmup():
    out = build_features(_bars(320))
    row = out.iloc[-1]
    for col in ("ema20", "ema50", "ema200", "rsi", "atr", "adx", "macd_hist",
                "bb_mid", "bb_upper", "bb_lower", "atr_percentile", "vwap",
                "last_swing_high", "last_swing_low", "pd_position",
                "range_position"):
        assert np.isfinite(float(row[col])), col
