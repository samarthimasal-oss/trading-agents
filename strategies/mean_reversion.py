"""Mean-reversion engine for range regimes.

Entry requires a quiet market (low ADX, compressed Bollinger width), a band
extreme, and a reversal candle.  The target is the Bollinger mid-band; if the
distance to the mean does not pay for the stop the trade is rejected.  This
engine is intentionally the most selective of the five and must not be used
against a strong trend.
"""
from __future__ import annotations

import math

from strategies.base import BaseStrategy, ExitDecision, PositionView, StrategyContext


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"
    defaults = {
        **BaseStrategy.defaults,
        "adx_max": 28.0,
        "bb_width_percentile_max": 0.90,
        "rsi_oversold": 40.0,
        "rsi_overbought": 60.0,
        "bb_position_buy": 0.20,
        "bb_position_sell": 0.80,
        "atr_stop": 1.10,
        "rr_target": 1.60,
        "min_rr": 0.80,
        "max_spread_atr": 0.12,
        "max_trend_distance_atr": 5.0,
        "min_bars": 60,
        "max_bars_in_trade": 16,
        "sessions": ["london", "overlap", "newyork"],
    }

    def _distance_from_ema200(self, ctx: StrategyContext) -> float:
        try:
            close = float(ctx.row["close"])
            ema200 = float(ctx.row["ema200"])
        except (KeyError, TypeError, ValueError):
            return float("nan")
        if not (_finite(close) and _finite(ema200)) or ctx.atr <= 0:
            return float("nan")
        return abs(close - ema200) / ctx.atr

    def evaluate(self, ctx: StrategyContext):
        if not self._ready(ctx):
            return []
        row = ctx.row
        atr = ctx.atr
        close = ctx.close
        try:
            open_ = float(row["open"])
            prev_close = float(ctx.prev["close"])
            adx = float(row["adx"])
            rsi = float(row["rsi"])
            rsi_slope = float(row["rsi_slope"])
            bb_mid = float(row["bb_mid"])
            bb_position = float(row["bb_position"])
            bb_width_pct = float(row["bb_width_percentile"])
        except (KeyError, TypeError, ValueError):
            return []
        if not all(math.isfinite(x) for x in
                    (open_, prev_close, adx, rsi, rsi_slope, bb_mid,
                     bb_position, bb_width_pct)):
            return []

        if adx > float(self.params["adx_max"]):
            return []
        if bb_width_pct > float(self.params["bb_width_percentile_max"]):
            return []
        trend_distance = self._distance_from_ema200(ctx)
        if _finite(trend_distance) and trend_distance > float(self.params["max_trend_distance_atr"]):
            return []
        if bb_mid <= 0:
            return []

        rr_target = float(self.params["rr_target"])
        atr_stop = float(self.params["atr_stop"])
        min_rr = float(self.params["min_rr"])

        # ----------------------------------------------------------- long bounce
        if (bb_position <= float(self.params["bb_position_buy"])
                and rsi <= float(self.params["rsi_oversold"])
                and (close > open_ or (rsi_slope > 0 and close >= prev_close))):
            recent_low = float(ctx.recent(4)["low"].min())
            stop = min(recent_low, close - atr_stop * atr)
            if stop < close:
                risk = close - stop
                target = bb_mid
                rr = (target - close) / risk if risk > 0 else 0.0
                if rr >= min_rr:
                    conf = 0.40
                    conf += 0.15 * (1.0 - bb_position)
                    conf += 0.15 * max(0.0, min(1.0,
                                             (float(self.params["rsi_oversold"]) - rsi) / 12.0 + 0.5))
                    conf += 0.10 * max(0.0, 1.0 - adx / float(self.params["adx_max"]))
                    conf += 0.10 * max(0.0, 1.0 - bb_width_pct /
                                       max(1e-9, float(self.params["bb_width_percentile_max"])))
                    sig = ctx.make_signal(
                        self.name, 1, close, stop, target,
                        self._clamp_confidence(conf),
                        evidence=[
                            f"ADX {adx:.1f} (range regime)",
                            f"RSI {rsi:.1f} turning up, BB position {bb_position:.2f}",
                            f"BB width percentile {bb_width_pct:.2f}",
                            f"target = mid-band {bb_mid:.5f}",
                        ],
                        invalidators=["close below the reversal low",
                                      "ADX expands above 30"],
                        regime="range", expires_bars=2, min_rr=min_rr,
                    )
                    if sig:
                        return [sig]

        # ---------------------------------------------------------- short bounce
        if (bb_position >= float(self.params["bb_position_sell"])
                and rsi >= float(self.params["rsi_overbought"])
                and (close < open_ or (rsi_slope < 0 and close <= prev_close))):
            recent_high = float(ctx.recent(4)["high"].max())
            stop = max(recent_high, close + atr_stop * atr)
            if stop > close:
                risk = stop - close
                target = bb_mid
                rr = (close - target) / risk if risk > 0 else 0.0
                if rr >= min_rr:
                    conf = 0.40
                    conf += 0.15 * bb_position
                    conf += 0.15 * max(0.0, min(1.0,
                                             (rsi - float(self.params["rsi_overbought"])) / 12.0 + 0.5))
                    conf += 0.10 * max(0.0, 1.0 - adx / float(self.params["adx_max"]))
                    conf += 0.10 * max(0.0, 1.0 - bb_width_pct /
                                       max(1e-9, float(self.params["bb_width_percentile_max"])))
                    sig = ctx.make_signal(
                        self.name, -1, close, stop, target,
                        self._clamp_confidence(conf),
                        evidence=[
                            f"ADX {adx:.1f} (range regime)",
                            f"RSI {rsi:.1f} turning down, BB position {bb_position:.2f}",
                            f"BB width percentile {bb_width_pct:.2f}",
                            f"target = mid-band {bb_mid:.5f}",
                        ],
                        invalidators=["close above the reversal high",
                                      "ADX expands above 30"],
                        regime="range", expires_bars=2, min_rr=min_rr,
                    )
                    if sig:
                        return [sig]
        return []

    def should_exit(self, position: PositionView, ctx: StrategyContext) -> ExitDecision:
        row = ctx.row
        try:
            close = float(row["close"])
            bb_mid = float(row["bb_mid"])
            rsi = float(row["rsi"])
        except (KeyError, TypeError, ValueError):
            return ExitDecision()
        if position.direction > 0:
            if close >= bb_mid or rsi >= 55.0:
                return ExitDecision("exit", "reverted to the mean")
        else:
            if close <= bb_mid or rsi <= 45.0:
                return ExitDecision("exit", "reverted to the mean")
        return ExitDecision()
