"""Session/range breakout engine.

Deterministic levels only:
  * London session breaks the completed Asia range (00:00-07:00 UTC).
  * New York/overlap breaks the completed London range (07:00-12:00 UTC).
  * If that range is unavailable, previous-day high/low is used.

The breakout bar must close beyond the level with an ATR buffer, inside a
compressed range, with volume expansion.  No retest is assumed.
"""
from __future__ import annotations

import math

from strategies.base import BaseStrategy, ExitDecision, PositionView, StrategyContext


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class BreakoutStrategy(BaseStrategy):
    name = "breakout"
    defaults = {
        **BaseStrategy.defaults,
        "buffer_atr": 0.10,
        "min_range_atr": 0.30,
        "max_range_atr": 2.50,
        "compression_percentile": 0.70,
        "volume_z_min": -0.5,
        "atr_stop": 1.0,
        "rr_target": 2.0,
        "max_breakout_atr": 3.0,
        "min_rr": 1.5,
        "max_spread_atr": 0.15,
        "min_bars": 60,
        "max_bars_in_trade": 20,
        "sessions": ["london", "overlap", "newyork"],
    }

    def _levels(self, ctx: StrategyContext):
        row = ctx.row
        session = ctx.session
        if session == "london":
            candidates = [("asia_high", "asia_low", "Asia range")]
        elif session in ("overlap", "newyork"):
            candidates = [("london_high", "london_low", "London range"),
                          ("asia_high", "asia_low", "Asia range")]
        else:
            return None
        candidates.append(("pd_high", "pd_low", "Previous day"))
        for high_col, low_col, label in candidates:
            high = row.get(high_col)
            low = row.get(low_col)
            if _finite(high) and _finite(low) and float(high) > float(low):
                return float(high), float(low), label
        return None

    def evaluate(self, ctx: StrategyContext):
        if not self._ready(ctx):
            return []
        levels = self._levels(ctx)
        if levels is None:
            return []
        level_high, level_low, source = levels
        row, prev = ctx.row, ctx.prev
        atr = ctx.atr
        try:
            close = float(row["close"])
            prev_close = float(prev["close"])
            vol_z = float(row["vol_z"]) if _finite(row.get("vol_z")) else 0.0
            atr_pct = float(row["atr_percentile"]) if _finite(row.get("atr_percentile")) else None
        except (KeyError, TypeError, ValueError):
            return []

        width = level_high - level_low
        range_atr = width / atr
        min_range = float(self.params["min_range_atr"])
        max_range = float(self.params["max_range_atr"])
        if not (min_range <= range_atr <= max_range):
            return []
        if atr_pct is not None and atr_pct > float(self.params["compression_percentile"]):
            return []
        if vol_z < float(self.params["volume_z_min"]):
            return []

        buffer = float(self.params["buffer_atr"]) * atr
        max_break = float(self.params["max_breakout_atr"]) * atr
        atr_stop = float(self.params["atr_stop"])
        rr_target = float(self.params["rr_target"])
        min_rr = float(self.params["min_rr"])

        if close > level_high + buffer and prev_close <= level_high + buffer:
            extension = close - (level_high + buffer)
            if extension > max_break:
                return []
            stop = min(level_high - 0.2 * atr, close - atr_stop * atr)
            if stop >= close:
                return []
            risk = close - stop
            target = close + rr_target * risk
            conf = 0.40
            conf += 0.20 * min(1.0, max(0.0, vol_z) / 2.0)
            conf += 0.20 * (1.0 - min(1.0, range_atr / max_range))
            if atr_pct is not None:
                conf += 0.10 * (1.0 - min(1.0, atr_pct /
                                         max(1e-9, float(self.params["compression_percentile"]))))
            conf += 0.10 * min(1.0, max(0.0, extension / max_break))
            return [s for s in [ctx.make_signal(
                self.name, 1, close, stop, target, self._clamp_confidence(conf),
                evidence=[f"{source} high {level_high:.2f}",
                          f"range {range_atr:.2f} ATR",
                          f"volume z {vol_z:+.2f}",
                          f"{ctx.session} session"],
                invalidators=["close back inside the range",
                              "volume expansion fades"],
                regime="breakout", expires_bars=3, min_rr=min_rr,
            )] if s]

        if close < level_low - buffer and prev_close >= level_low - buffer:
            extension = (level_low - buffer) - close
            if extension > max_break:
                return []
            stop = max(level_low + 0.2 * atr, close + atr_stop * atr)
            if stop <= close:
                return []
            risk = stop - close
            target = close - rr_target * risk
            conf = 0.40
            conf += 0.20 * min(1.0, max(0.0, vol_z) / 2.0)
            conf += 0.20 * (1.0 - min(1.0, range_atr / max_range))
            if atr_pct is not None:
                conf += 0.10 * (1.0 - min(1.0, atr_pct /
                                         max(1e-9, float(self.params["compression_percentile"]))))
            conf += 0.10 * min(1.0, max(0.0, extension / max_break))
            return [s for s in [ctx.make_signal(
                self.name, -1, close, stop, target, self._clamp_confidence(conf),
                evidence=[f"{source} low {level_low:.2f}",
                          f"range {range_atr:.2f} ATR",
                          f"volume z {vol_z:+.2f}",
                          f"{ctx.session} session"],
                invalidators=["close back inside the range",
                              "volume expansion fades"],
                regime="breakout", expires_bars=3, min_rr=min_rr,
            )] if s]
        return []

    def should_exit(self, position: PositionView, ctx: StrategyContext) -> ExitDecision:
        return ExitDecision()
