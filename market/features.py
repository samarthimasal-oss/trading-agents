"""Causal feature engineering for the strategy engines.

Every column at row ``i`` is computed from rows ``0..i`` only.  The no-lookahead
test in ``tests/test_features_causality.py`` protects this property by mutating
future bars and asserting that past feature rows do not change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from market.structure import add_structure_features

DEFAULT_FEATURE_CONFIG: dict = {
    "ema_fast": 12,
    "ema_slow": 34,
    "ema20": 20,
    "ema50": 50,
    "ema200": 200,
    "rsi_period": 14,
    "atr_period": 14,
    "adx_period": 14,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bb_period": 20,
    "bb_std": 2.0,
    "volume_window": 20,
    "percentile_window": 96,
    "swing_lookback": 2,
    "displacement_atr": 1.0,
}

SESSIONS = ("asia", "london", "overlap", "newyork", "off")


def _ema(series: pd.Series, span: int) -> pd.Series:
    span = max(1, int(span))
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    period = max(2, int(period))
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_loss == 0.0) & (avg_gain == 0.0)), 50.0)
    return out


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    period = max(2, int(period))
    return _true_range(df).ewm(alpha=1.0 / period, adjust=False,
                               min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    period = max(2, int(period))
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0.0), up, 0.0), index=df.index)
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0.0), down, 0.0), index=df.index)
    atr = _atr(df, period)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False,
                                  min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False,
                                    min_periods=period).mean() / atr
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    window = max(2, int(window))
    return series.rolling(window, min_periods=window).apply(
        lambda values: float(np.mean(values <= values[-1])), raw=True)


def _zscore(series: pd.Series, window: int) -> pd.Series:
    window = max(2, int(window))
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def _session_label(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "overlap"
    if 16 <= hour < 21:
        return "newyork"
    return "off"


def add_sessions(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "hour_utc" not in out.columns:
        if "time" not in out.columns:
            raise ValueError("features require a 'time' column")
        dt = pd.to_datetime(out["time"], unit="s", utc=True)
        out["hour_utc"] = dt.dt.hour.astype(int)
    out["session"] = out["hour_utc"].map(_session_label)
    out["is_trade_session"] = out["session"].isin(
        ["london", "overlap", "newyork"])
    return out


def add_previous_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Previous UTC day's high/low/mid, shifted so today cannot see today."""
    out = df.copy()
    if "day_id" not in out.columns:
        out["day_id"] = (out["time"] // 86_400).astype("int64")
    daily = out.groupby("day_id").agg(
        pd_high=("high", "max"),
        pd_low=("low", "min"),
        pd_close=("close", "last"),
    )
    daily = daily.shift(1)
    daily["pd_mid"] = (daily["pd_high"] + daily["pd_low"]) / 2.0
    daily["pd_range"] = daily["pd_high"] - daily["pd_low"]
    out = out.merge(daily, left_on="day_id", right_index=True, how="left")
    out["pd_position"] = (
        (out["close"] - out["pd_low"]) / out["pd_range"].where(out["pd_range"] > 0)
    ).clip(0.0, 1.0)
    return out


def add_session_ranges(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative high/low of the Asia and London windows.

    The value at the first bar after the window is the completed window range;
    it is never the full day's range at an earlier bar.
    """
    out = df.copy()
    day = out["day_id"]
    asia = out["session"].eq("asia")
    london = out["session"].eq("london")
    out["asia_high"] = out["high"].where(asia).groupby(day).transform(
        lambda s: s.ffill().cummax())
    out["asia_low"] = out["low"].where(asia).groupby(day).transform(
        lambda s: s.ffill().cummin())
    out["london_high"] = out["high"].where(london).groupby(day).transform(
        lambda s: s.ffill().cummax())
    out["london_low"] = out["low"].where(london).groupby(day).transform(
        lambda s: s.ffill().cummin())
    out["asia_range"] = out["asia_high"] - out["asia_low"]
    out["london_range"] = out["london_high"] - out["london_low"]
    return out


def build_features(df: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Return a copy of ``df`` with every causal feature column added."""
    cfg = {**DEFAULT_FEATURE_CONFIG, **(config or {})}
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        if col not in out.columns:
            raise ValueError(f"features require a '{col}' column")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "tick_volume" not in out.columns:
        out["tick_volume"] = 0.0
    out["tick_volume"] = pd.to_numeric(out["tick_volume"], errors="coerce").fillna(0.0)
    if "time" not in out.columns:
        raise ValueError("features require a 'time' column")
    out["day_id"] = (out["time"] // 86_400).astype("int64")
    out["hour_utc"] = pd.to_datetime(
        out["time"], unit="s", utc=True).dt.hour.astype(int)

    close = out["close"]
    out["ema_fast"] = _ema(close, cfg["ema_fast"])
    out["ema_slow"] = _ema(close, cfg["ema_slow"])
    out["ema20"] = _ema(close, cfg["ema20"])
    out["ema50"] = _ema(close, cfg["ema50"])
    out["ema200"] = _ema(close, cfg["ema200"])
    out["rsi"] = _rsi(close, cfg["rsi_period"])
    out["atr"] = _atr(out, cfg["atr_period"])
    out["adx"] = _adx(out, cfg["adx_period"])
    out["atr_pct"] = out["atr"] / close.replace(0.0, np.nan)
    out["atr_percentile"] = _rolling_percentile(
        out["atr"], cfg["percentile_window"])
    out["range_atr"] = (out["high"] - out["low"]) / out["atr"].replace(0.0, np.nan)
    out["ema20_slope"] = out["ema20"] - out["ema20"].shift(10)
    out["ema20_slope_atr"] = out["ema20_slope"] / out["atr"].replace(0.0, np.nan)
    out["ema50_slope"] = out["ema50"] - out["ema50"].shift(10)

    for period in (3, 5, 10):
        out[f"roc_{period}"] = close.pct_change(period)

    ema_fast = close.ewm(span=int(cfg["macd_fast"]), adjust=False,
                         min_periods=int(cfg["macd_fast"])).mean()
    ema_slow = close.ewm(span=int(cfg["macd_slow"]), adjust=False,
                         min_periods=int(cfg["macd_slow"])).mean()
    out["macd"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd"].ewm(
        span=int(cfg["macd_signal"]), adjust=False,
        min_periods=int(cfg["macd_signal"])).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["macd_hist_slope"] = out["macd_hist"] - out["macd_hist"].shift(1)
    out["rsi_slope"] = out["rsi"] - out["rsi"].shift(1)

    bb_period = int(cfg["bb_period"])
    bb_mid = close.rolling(bb_period, min_periods=bb_period).mean()
    bb_std = close.rolling(bb_period, min_periods=bb_period).std(ddof=0)
    out["bb_mid"] = bb_mid
    out["bb_upper"] = bb_mid + float(cfg["bb_std"]) * bb_std
    out["bb_lower"] = bb_mid - float(cfg["bb_std"]) * bb_std
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / bb_mid.replace(0.0, np.nan)
    out["bb_width_percentile"] = _rolling_percentile(
        out["bb_width"], cfg["percentile_window"])
    out["bb_position"] = (
        (close - out["bb_lower"])
        / (out["bb_upper"] - out["bb_lower"]).replace(0.0, np.nan)
    ).clip(0.0, 1.0)

    out["vol_z"] = _zscore(out["tick_volume"], cfg["volume_window"]).fillna(0.0)

    out = add_sessions(out)
    out = add_previous_day_levels(out)
    out = add_session_ranges(out)

    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = typical * out["tick_volume"]
    denom = out["tick_volume"].groupby(out["day_id"]).cumsum().replace(0.0, np.nan)
    out["vwap"] = pv.groupby(out["day_id"]).cumsum() / denom

    out = add_structure_features(
        out,
        swing_lookback=int(cfg["swing_lookback"]),
        displacement_atr=float(cfg["displacement_atr"]),
    )
    return out
