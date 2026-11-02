"""Quantitative agents: three independent opinions turned into one consensus.

All indicator math is pure pandas/numpy so the exact same code runs in the
live pipeline and in the backtester.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from core.models import Candle, MarketContext, Consensus, Signal


# ------------------------------------------------------------------ indicators
def to_frame(bars: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame([b.__dict__ for b in bars])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def enrich(df: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add indicator columns to the close-price series and ATR to the frame."""
    closes = ema(df["close"], p["ema_fast"]), ema(df["close"], p["ema_slow"])
    df = df.copy()
    df["ema_fast"], df["ema_slow"] = closes
    df["rsi"] = rsi(df["close"], p["rsi_period"])
    df["atr"] = atr(df, p["atr_period"])
    return df


def latest(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-1]


# -------------------------------------------------------------------- agents
class TrendAgent:
    """EMA fast/slow crossover with slope filter."""
    name = "trend"

    def analyze(self, df: pd.DataFrame, conf) -> Signal:
        row = latest(df)
        fast, slow = row["ema_fast"], row["ema_slow"]
        diff = fast - slow
        atr_val = row["atr"]
        direction = 1 if diff > 0 else (-1 if diff < 0 else 0)
        # confidence: separation of moving averages relative to volatility
        sep = abs(diff) / atr_val if atr_val and atr_val > 0 else 0.0
        confident = float(np.clip(sep / 0.75, 0.05, 0.95))
        reason = f"EMA{fast:.5f} vs EMA{slow:.5f} (sep {sep:.2f} ATR)"
        return Signal(self.name, direction, conf["weight_trend"], confident, reason)


class MomentumAgent:
    """RSI with deviation bands + short-term rate of change."""
    name = "momentum"

    def analyze(self, df: pd.DataFrame, conf) -> Signal:
        row = latest(df)
        r = row["rsi"]
        roc = df["close"].pct_change(6).iloc[-1]  # momentum over last 6 bars
        direction, why = 0, ""
        if r < conf["rsi_oversold"]:
            direction, why = 1, f"RSI oversold ({r:.1f}) + roc {roc:+.2%}"
        elif r > conf["rsi_overbought"]:
            direction, why = -1, f"RSI overbought ({r:.1f}) + roc {roc:+.2%}"
        else:
            # inside the band: follow short-term momentum mildly
            if roc > 0.0015 and r > 55:
                direction, why = 1, f"thrust up: roc {roc:+.2%}, RSI {r:.1f}"
            elif roc < -0.0015 and r < 45:
                direction, why = -1, f"thrust down: roc {roc:+.2%}, RSI {r:.1f}"
            else:
                why = f"neutral RSI {r:.1f}, roc {roc:+.2%}"
        confident = float(np.clip(abs(r - 50) / 35, 0.05, 0.9))
        return Signal(self.name, direction, conf["weight_momentum"], confident, why)


class ReversionAgent:
    """Mean-reversion: price stretched from its EMA (z-score-ish)."""
    name = "reversion"

    def analyze(self, df: pd.DataFrame, conf) -> Signal:
        row = latest(df)
        atr_val = row["atr"]
        if not atr_val or atr_val <= 0:
            return Signal(self.name, 0, conf["weight_reversion"], 0.05, "no ATR")
        stretch = (row["close"] - row["ema_slow"]) / atr_val   # ATR units from slow EMA
        direction, why = 0, f"stretch {stretch:+.2f} ATR from slow EMA"
        if stretch > 2.2:
            direction, why = -1, f"overextended long (stretch {stretch:+.2f} ATR)"
        elif stretch < -2.2:
            direction, why = 1, f"overextended short (stretch {stretch:+.2f} ATR)"
        confident = float(np.clip((abs(stretch) - 1.0) / 1.5, 0.05, 0.85))
        return Signal(self.name, direction, conf["weight_reversion"], confident, why)


class BreakoutAgent:
    """Donchian-style range breakout over N bars."""

    name = "breakout"

    def analyze(self, df: pd.DataFrame, conf) -> Signal:
        row = latest(df)
        atr_val = row["atr"]
        look = int(conf.get("breakout_lookback", 24))
        if not atr_val or atr_val <= 0 or len(df) < look + 2:
            return Signal(self.name, 0, conf["weight_breakout"], 0.05, "warming up")
        window = df.tail(look + 1)
        hh = window["high"].max()
        ll = window["low"].min()
        prev_hh = window["high"].iloc[:-1].max()
        prev_ll = window["low"].iloc[:-1].min()
        close = row["close"]
        direction, why = 0, f"{look}-bar range {prev_ll:.5f}..{prev_hh:.5f}"
        if close > prev_hh:
            direction, why = 1, (f"broke {look}-bar high {prev_hh:.5f} "
                                 f"by {(close - prev_hh) / atr_val:.2f} ATR")
        elif close < prev_ll:
            direction, why = -1, (f"broke {look}-bar low {prev_ll:.5f} "
                                  f"by {(prev_ll - close) / atr_val:.2f} ATR")
        confident = float(np.clip(abs(close - (hh + ll) / 2) / atr_val / 2.0, 0.15, 0.75))
        return Signal(self.name, direction, conf["weight_breakout"], confident, why)


class PullbackAgent:
    """Trade with the major trend, entering on a pullback to the EMA20."""
    name = "pullback"

    def analyze(self, df: pd.DataFrame, conf) -> Signal:
        close = df["close"]
        e20 = ema(close, 20).iloc[-1]
        e200 = ema(close, 200).iloc[-1] if len(df) > 210 else close.iloc[-1]
        slope = ema(close, 200).diff(12).iloc[-1] if len(df) > 222 else 0.0
        row = latest(df)
        atr_val = row["atr"] or (e20 * 0.001) or 1e-9
        lows_touched = bool((df["low"].tail(4) <= e20 * 1.0002).any())
        close_above = row["close"] > e20 * 0.9995
        rallied = bool((df["close"].tail(4) >= e20).any())
        closed_below = row["close"] < e20 * 1.0005

        direction, why = 0, f"E20 {e20:.5f} slope200 {slope:+.2e}"
        if slope > 0 and lows_touched and close_above:
            direction = 1
            why = f"uptrend pullback to E20 ({e20:.5f}) held"
        elif slope < 0 and rallied and closed_below:
            direction = -1
            why = f"downtrend pullback to E20 ({e20:.5f}) rejected"
        # confidence by trend quality: |E20-E200| in ATR units
        quality = float(np.clip(abs(e20 - e200) / atr_val / 3.0, 0.05, 0.7))
        return Signal(self.name, direction, conf["weight_pullback"], quality, why)


AGENTS = [TrendAgent(), MomentumAgent(), ReversionAgent(), BreakoutAgent(),
          PullbackAgent()]


def build_consensus(signals: list[Signal], threshold: float) -> Consensus:
    strength = sum(s.weight * s.confidence * s.direction for s in signals)
    norm = {"strength": float(np.tanh(strength))}   # squash to -1..1
    total_weight = sum(s.weight for s in signals) or 1.0
    confidence = float(np.clip(
        sum(s.confidence * s.weight for s in signals) / total_weight, 0.0, 1.0))
    active = [s for s in signals if s.direction != 0]
    majority_directions = {s.direction for s in active}
    if len(majority_directions) == 1 and abs(norm["strength"]) >= threshold:
        direction = int(next(iter(majority_directions)))
    elif abs(norm["strength"]) >= threshold:
        direction = 1 if norm["strength"] > 0 else -1
    else:
        direction = 0
    return Consensus(direction=direction, strength=norm["strength"],
                     confidence=round(confidence, 3), signals=signals)


def run_quant_agents(df: pd.DataFrame, conf: dict) -> Consensus:
    signals = [a.analyze(df, conf) for a in AGENTS]
    return build_consensus(signals, conf["consensus_threshold"])


def context_with_indicators(ctx: MarketContext, bars: list[Candle],
                            agent_cfg: dict) -> pd.DataFrame:
    """Attach the computed indicators back onto the context for reuse."""
    df = enrich(to_frame(bars), agent_cfg)
    row = latest(df)
    ctx.ema_fast, ctx.ema_slow = float(row["ema_fast"]), float(row["ema_slow"])
    ctx.rsi, ctx.atr = float(row["rsi"]), float(row["atr"])
    return df


def apply_closed_indicators(ctx: MarketContext,
                            closed_df: pd.DataFrame) -> MarketContext:
    """Overwrite context indicators with the last **closed** bar.

    ``context_with_indicators`` sees the full frame, including the forming
    candle, which is fine for display but not for sizing or stops.  Call this
    after slicing the last row off and before building a trade plan.
    """
    if closed_df is None or closed_df.empty:
        return ctx
    row = closed_df.iloc[-1]
    ctx.ema_fast = float(row["ema_fast"])
    ctx.ema_slow = float(row["ema_slow"])
    ctx.rsi = float(row["rsi"])
    ctx.atr = float(row["atr"])
    return ctx
