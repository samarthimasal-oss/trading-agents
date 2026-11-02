"""Deterministic market structure: swings, BOS/CHoCH, sweeps, FVG, OB.

Every function is causal.  Where a pattern needs future confirmation (a swing
high is only a swing once the following bars fail to exceed it), the level is
shifted forward by the confirmation lag before it is exposed.  A strategy
reading row ``i`` can therefore never see information from row ``i+1``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    period = max(2, int(period))
    if "atr" in df.columns:
        atr = pd.to_numeric(df["atr"], errors="coerce")
    else:
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False,
                     min_periods=period).mean()
    # early bars have no Wilder ATR yet; a simple range average keeps the
    # structure detectors usable without introducing future data
    proxy = (df["high"] - df["low"]).rolling(period, min_periods=1).mean()
    return atr.fillna(proxy)


def confirmed_swings(df: pd.DataFrame, lookback: int = 2) -> pd.DataFrame:
    """Add swing levels confirmed only after ``lookback`` bars.

    ``last_swing_high`` at row ``i`` is the most recent swing high that is
    already confirmed by the close of row ``i``.
    """
    out = df.copy()
    lookback = max(1, int(lookback))
    window = 2 * lookback + 1
    roll_high = out["high"].rolling(window, center=True, min_periods=window).max()
    roll_low = out["low"].rolling(window, center=True, min_periods=window).min()
    is_high = out["high"].eq(roll_high)
    is_low = out["low"].eq(roll_low)

    confirmed_high = is_high.shift(lookback).fillna(False).astype(bool)
    confirmed_low = is_low.shift(lookback).fillna(False).astype(bool)
    out["swing_high_confirmed"] = confirmed_high
    out["swing_low_confirmed"] = confirmed_low
    out["swing_high_level"] = out["high"].shift(lookback).where(confirmed_high)
    out["swing_low_level"] = out["low"].shift(lookback).where(confirmed_low)
    out["last_swing_high"] = out["swing_high_level"].ffill()
    out["last_swing_low"] = out["swing_low_level"].ffill()
    return out


def market_structure(
    df: pd.DataFrame,
    lookback: int = 2,
    displacement_atr: float = 1.0,
) -> pd.DataFrame:
    """Add BOS, CHoCH, liquidity sweeps and displacement flags."""
    if "last_swing_high" not in df.columns or "last_swing_low" not in df.columns:
        out = confirmed_swings(df, lookback)
    else:
        out = df.copy()

    close = out["close"].to_numpy(dtype=float)
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    open_ = out["open"].to_numpy(dtype=float)
    last_high = out["last_swing_high"].to_numpy(dtype=float)
    last_low = out["last_swing_low"].to_numpy(dtype=float)
    atr = _atr_series(out).to_numpy(dtype=float)
    n = len(out)

    bos_up = np.zeros(n, dtype=bool)
    bos_down = np.zeros(n, dtype=bool)
    choch_up = np.zeros(n, dtype=bool)
    choch_down = np.zeros(n, dtype=bool)
    direction = 0
    for i in range(n):
        if i == 0 or not np.isfinite(last_high[i]) or not np.isfinite(last_low[i]):
            continue
        prev_close = close[i - 1]
        if close[i] > last_high[i] and prev_close <= last_high[i]:
            bos_up[i] = True
            if direction < 0:
                choch_up[i] = True
            direction = 1
        elif close[i] < last_low[i] and prev_close >= last_low[i]:
            bos_down[i] = True
            if direction > 0:
                choch_down[i] = True
            direction = -1

    sweep_high = (high > last_high) & (close < last_high) & np.isfinite(last_high)
    sweep_low = (low < last_low) & (close > last_low) & np.isfinite(last_low)

    body = np.abs(close - open_)
    disp = body > displacement_atr * np.where(np.isfinite(atr) & (atr > 0), atr, np.inf)
    displacement_up = disp & (close > open_)
    displacement_down = disp & (close < open_)

    out["bos_up"] = bos_up
    out["bos_down"] = bos_down
    out["choch_up"] = choch_up
    out["choch_down"] = choch_down
    out["sweep_high"] = sweep_high
    out["sweep_low"] = sweep_low
    out["sweep_high_level"] = np.where(sweep_high, last_high, np.nan)
    out["sweep_low_level"] = np.where(sweep_low, last_low, np.nan)
    out["displacement_up"] = displacement_up
    out["displacement_down"] = displacement_down
    out["body_atr"] = body / np.where(np.isfinite(atr) & (atr > 0), atr, np.nan)
    return out


def fair_value_gaps(df: pd.DataFrame, max_zones: int = 20) -> pd.DataFrame:
    """Track the most recent active bullish/bearish fair value gaps.

    A bullish FVG is the three-candle imbalance ``low[i] > high[i-2]``.  The
    zone stays active until price fully trades through its far side; the
    strategy layer uses the nearest active zone, never a future one.
    """
    out = df.copy()
    n = len(out)
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)

    fvg_bull = np.zeros(n, dtype=bool)
    fvg_bear = np.zeros(n, dtype=bool)
    bull_top = np.full(n, np.nan)
    bull_bottom = np.full(n, np.nan)
    bull_age = np.full(n, np.nan)
    bear_top = np.full(n, np.nan)
    bear_bottom = np.full(n, np.nan)
    bear_age = np.full(n, np.nan)

    bulls: list[dict] = []
    bears: list[dict] = []
    for i in range(2, n):
        bulls = [z for z in bulls
                 if not (low[i] <= z["bottom"] or close[i] < z["bottom"])]
        bears = [z for z in bears
                 if not (high[i] >= z["top"] or close[i] > z["top"])]

        if low[i] > high[i - 2]:
            bulls.append({"created": i, "top": float(low[i]),
                          "bottom": float(high[i - 2])})
            fvg_bull[i] = True
        if high[i] < low[i - 2]:
            bears.append({"created": i, "top": float(low[i - 2]),
                          "bottom": float(high[i])})
            fvg_bear[i] = True

        bulls = bulls[-max_zones:]
        bears = bears[-max_zones:]
        if bulls:
            z = bulls[-1]
            bull_top[i] = z["top"]
            bull_bottom[i] = z["bottom"]
            bull_age[i] = i - z["created"]
        if bears:
            z = bears[-1]
            bear_top[i] = z["top"]
            bear_bottom[i] = z["bottom"]
            bear_age[i] = i - z["created"]

    out["fvg_bull"] = fvg_bull
    out["fvg_bear"] = fvg_bear
    out["fvg_bull_top"] = bull_top
    out["fvg_bull_bottom"] = bull_bottom
    out["fvg_bull_age"] = bull_age
    out["fvg_bear_top"] = bear_top
    out["fvg_bear_bottom"] = bear_bottom
    out["fvg_bear_age"] = bear_age
    return out


def order_blocks(
    df: pd.DataFrame,
    displacement_atr: float = 1.0,
    max_zones: int = 20,
) -> pd.DataFrame:
    """Track the last opposing candle before a displacement move."""
    out = df.copy()
    n = len(out)
    open_ = out["open"].to_numpy(dtype=float)
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    atr = _atr_series(out).to_numpy(dtype=float)

    ob_bull = np.zeros(n, dtype=bool)
    ob_bear = np.zeros(n, dtype=bool)
    bull_top = np.full(n, np.nan)
    bull_bottom = np.full(n, np.nan)
    bull_age = np.full(n, np.nan)
    bear_top = np.full(n, np.nan)
    bear_bottom = np.full(n, np.nan)
    bear_age = np.full(n, np.nan)

    bulls: list[dict] = []
    bears: list[dict] = []
    for i in range(1, n):
        bulls = [z for z in bulls if close[i] >= z["bottom"]]
        bears = [z for z in bears if close[i] <= z["top"]]
        a = atr[i]
        if np.isfinite(a) and a > 0:
            body = close[i] - open_[i]
            if (body > displacement_atr * a
                    and close[i] > high[i - 1]
                    and close[i - 1] < open_[i - 1]):
                bulls.append({"created": i, "top": float(high[i - 1]),
                              "bottom": float(low[i - 1])})
                ob_bull[i] = True
            if (-body > displacement_atr * a
                    and close[i] < low[i - 1]
                    and close[i - 1] > open_[i - 1]):
                bears.append({"created": i, "top": float(high[i - 1]),
                              "bottom": float(low[i - 1])})
                ob_bear[i] = True
        bulls = bulls[-max_zones:]
        bears = bears[-max_zones:]
        if bulls:
            z = bulls[-1]
            bull_top[i] = z["top"]
            bull_bottom[i] = z["bottom"]
            bull_age[i] = i - z["created"]
        if bears:
            z = bears[-1]
            bear_top[i] = z["top"]
            bear_bottom[i] = z["bottom"]
            bear_age[i] = i - z["created"]

    out["ob_bull"] = ob_bull
    out["ob_bear"] = ob_bear
    out["ob_bull_top"] = bull_top
    out["ob_bull_bottom"] = bull_bottom
    out["ob_bull_age"] = bull_age
    out["ob_bear_top"] = bear_top
    out["ob_bear_bottom"] = bear_bottom
    out["ob_bear_age"] = bear_age
    return out


def premium_discount(df: pd.DataFrame) -> pd.DataFrame:
    """Where price sits inside the last confirmed swing range (0..1)."""
    out = df.copy()
    if "last_swing_high" not in out.columns or "last_swing_low" not in out.columns:
        out = confirmed_swings(out)
    high = pd.to_numeric(out["last_swing_high"], errors="coerce")
    low = pd.to_numeric(out["last_swing_low"], errors="coerce")
    rng = high - low
    out["range_high"] = high
    out["range_low"] = low
    out["range_mid"] = (high + low) / 2.0
    out["range_size"] = rng
    out["range_position"] = ((out["close"] - low) / rng.where(rng > 0)).clip(0.0, 1.0)
    return out


def add_structure_features(
    df: pd.DataFrame,
    *,
    swing_lookback: int = 2,
    displacement_atr: float = 1.0,
) -> pd.DataFrame:
    """Apply the full structure stack once, without duplicating columns."""
    out = confirmed_swings(df, swing_lookback)
    out = market_structure(out, swing_lookback, displacement_atr)
    out = fair_value_gaps(out)
    out = order_blocks(out, displacement_atr)
    out = premium_discount(out)
    return out
