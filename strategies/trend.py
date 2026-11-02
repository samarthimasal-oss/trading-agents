"""Trend-continuation engine.

Rules (all on closed bars):
  * EMA20 > EMA50 > EMA200 (or the reverse) with a positive/negative slope.
  * ADX confirms directional strength and ROC confirms momentum.
  * Price has pulled back to EMA20/EMA50 and closed back in the trend.
  * Stop below the pullback swing; target at 2R.
"""
from __future__ import annotations

import math

from strategies.base import BaseStrategy, ExitDecision, PositionView, StrategyContext


class TrendStrategy(BaseStrategy):
    name = "trend"
    defaults = {
        **BaseStrategy.defaults,
        "ema_fast": 20,
        "ema_slow": 50,
        "ema_trend": 200,
        "adx_min": 20.0,
        "roc_min": 0.0,
        "atr_stop": 1.5,
        "rr_target": 2.0,
        "max_extension_atr": 2.5,
        "pullback_atr": 0.5,
        "min_rr": 1.5,
        "max_spread_atr": 0.15,
        "min_bars": 210,
        "max_bars_in_trade": 40,
        "sessions": ["london", "overlap", "newyork"],
    }

    def evaluate(self, ctx: StrategyContext):
        if not self._ready(ctx):
            return []
        row, prev = ctx.row, ctx.prev
        atr = ctx.atr
        try:
            ema20 = float(row["ema20"])
            ema50 = float(row["ema50"])
            ema200 = float(row["ema200"])
            adx = float(row["adx"])
            roc = float(row["roc_10"])
            slope = float(row["ema20_slope"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            return []
        if not all(math.isfinite(x) for x in (ema20, ema50, ema200, adx, roc, slope)):
            return []

        long_trend = ema20 > ema50 > ema200 and slope > 0 and roc > float(self.params["roc_min"])
        short_trend = ema20 < ema50 < ema200 and slope < 0 and roc < -float(self.params["roc_min"])
        if not (long_trend or short_trend):
            return []
        if adx < float(self.params["adx_min"]):
            return []

        recent = ctx.recent(6)
        lows = recent["low"].astype(float)
        highs = recent["high"].astype(float)
        pullback = float(self.params["pullback_atr"]) * atr
        max_ext = float(self.params["max_extension_atr"])
        atr_stop = float(self.params["atr_stop"])
        rr_target = float(self.params["rr_target"])
        min_rr = float(self.params["min_rr"])

        if long_trend:
            touched = (lows <= ema20 + pullback).any() or (lows <= ema50 + pullback).any()
            extension = (close - ema20) / atr
            if not touched or close < ema20 or extension > max_ext:
                return []
            stop = min(float(lows.min()), ema50 - 0.25 * atr, close - atr_stop * atr)
            if stop >= close:
                return []
            risk = close - stop
            target = close + rr_target * risk
            conf = 0.45
            conf += 0.20 * min(1.0, max(0.0, (adx - float(self.params["adx_min"])) / 25.0))
            conf += 0.15 * min(1.0, abs(roc) / 0.01)
            conf += 0.10 * min(1.0, abs(slope) / atr)
            conf += 0.10 * max(0.0, 1.0 - extension / max_ext)
            return [s for s in [ctx.make_signal(
                self.name, 1, close, stop, target, self._clamp_confidence(conf),
                evidence=[
                    f"EMA20 {ema20:.2f} > EMA50 {ema50:.2f} > EMA200 {ema200:.2f}",
                    f"ADX {adx:.1f}",
                    f"ROC10 {roc:+.3%}",
                    "pullback held EMA20/50",
                ],
                invalidators=["close below EMA50", "EMA20 crosses below EMA50",
                              "ADX falls below 15"],
                regime="trend", expires_bars=3, min_rr=min_rr,
            )] if s]

        touched = (highs >= ema20 - pullback).any() or (highs >= ema50 - pullback).any()
        extension = (ema20 - close) / atr
        if not touched or close > ema20 or extension > max_ext:
            return []
        stop = max(float(highs.max()), ema50 + 0.25 * atr, close + atr_stop * atr)
        if stop <= close:
            return []
        risk = stop - close
        target = close - rr_target * risk
        conf = 0.45
        conf += 0.20 * min(1.0, max(0.0, (adx - float(self.params["adx_min"])) / 25.0))
        conf += 0.15 * min(1.0, abs(roc) / 0.01)
        conf += 0.10 * min(1.0, abs(slope) / atr)
        conf += 0.10 * max(0.0, 1.0 - extension / max_ext)
        return [s for s in [ctx.make_signal(
            self.name, -1, close, stop, target, self._clamp_confidence(conf),
            evidence=[
                f"EMA20 {ema20:.2f} < EMA50 {ema50:.2f} < EMA200 {ema200:.2f}",
                f"ADX {adx:.1f}",
                f"ROC10 {roc:+.3%}",
                "pullback held EMA20/50",
            ],
            invalidators=["close above EMA50", "EMA20 crosses above EMA50",
                          "ADX falls below 15"],
            regime="trend", expires_bars=3, min_rr=min_rr,
        )] if s]

    def should_exit(self, position: PositionView, ctx: StrategyContext) -> ExitDecision:
        row = ctx.row
        try:
            ema20 = float(row["ema20"])
            ema50 = float(row["ema50"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            return ExitDecision()
        if position.direction > 0:
            if ema20 < ema50 or close < ema50:
                return ExitDecision("exit", "trend flipped below EMA50")
        else:
            if ema20 > ema50 or close > ema50:
                return ExitDecision("exit", "trend flipped above EMA50")
        return ExitDecision()
