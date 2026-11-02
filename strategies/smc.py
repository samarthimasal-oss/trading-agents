"""SMC / ICT engine.

The engine looks for a liquidity sweep, a displacement away from the sweep,
and an entry back into a fair-value gap or order block in the discount
(longs) or premium (shorts) half of the last confirmed swing range.

Every input column is produced causally by :mod:`market.structure`; the engine
only combines signals that are already confirmed at the close of the current
bar.
"""
from __future__ import annotations

import math

from strategies.base import BaseStrategy, ExitDecision, PositionView, StrategyContext


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class SmcStrategy(BaseStrategy):
    name = "smc"
    defaults = {
        **BaseStrategy.defaults,
        "sweep_lookback": 10,
        "structure_lookback": 4,
        "zone_atr": 0.60,
        "stop_buffer_atr": 0.35,
        "rr_target": 2.0,
        "min_rr": 1.5,
        "max_spread_atr": 0.18,
        "min_bars": 80,
        "max_bars_in_trade": 30,
        "max_range_position": 0.55,
        "sessions": ["london", "overlap", "newyork"],
    }

    # ------------------------------------------------------------------ helpers
    def _recent_level(self, recent, flag_col: str, level_col: str,
                      fallback_col: str) -> float:
        flagged = recent[recent[flag_col].astype(bool)]
        if not flagged.empty:
            levels = [float(v) for v in flagged[level_col] if _finite(v)]
            if levels:
                return levels[-1]
        values = [float(v) for v in recent[fallback_col] if _finite(v)]
        return min(values) if flag_col == "sweep_low" else max(values)

    def _zone(self, row, prefix: str) -> tuple[float, float] | None:
        top = row.get(f"{prefix}_top")
        bottom = row.get(f"{prefix}_bottom")
        if _finite(top) and _finite(bottom) and float(top) > float(bottom):
            return float(bottom), float(top)
        return None

    def _near(self, close: float, zone: tuple[float, float], atr: float) -> bool:
        bottom, top = zone
        tolerance = float(self.params["zone_atr"]) * atr
        return bottom - tolerance <= close <= top + tolerance

    def _displacement(self, recent, direction: int) -> bool:
        if direction > 0:
            return bool(recent["displacement_up"].any()) or bool(recent["bos_up"].any()) \
                or bool(recent["choch_up"].any())
        return bool(recent["displacement_down"].any()) or bool(recent["bos_down"].any()) \
            or bool(recent["choch_down"].any())

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, ctx: StrategyContext):
        if not self._ready(ctx):
            return []
        row = ctx.row
        atr = ctx.atr
        close = ctx.close
        lookback = max(3, int(self.params["sweep_lookback"]))
        struct_lookback = max(1, int(self.params["structure_lookback"]))
        recent = ctx.recent(lookback)
        structure = ctx.recent(struct_lookback)

        try:
            range_position = float(row.get("range_position"))
        except (TypeError, ValueError):
            range_position = float("nan")
        if not _finite(range_position):
            range_position = 0.5

        bull_zone = self._zone(row, "ob_bull") or self._zone(row, "fvg_bull")
        bear_zone = self._zone(row, "ob_bear") or self._zone(row, "fvg_bear")
        buffer = float(self.params["stop_buffer_atr"]) * atr
        rr_target = float(self.params["rr_target"])
        min_rr = float(self.params["min_rr"])
        max_pos = float(self.params["max_range_position"])

        # ------------------------------------------------------------ long setup
        swept_low = bool(recent["sweep_low"].any()) if "sweep_low" in recent else False
        if swept_low and self._displacement(structure, 1) and range_position <= max_pos:
            sweep_level = self._recent_level(recent, "sweep_low", "sweep_low_level", "low")
            zone_bottom = bull_zone[0] if bull_zone else sweep_level
            stop = min(sweep_level, zone_bottom) - buffer
            if stop < close:
                risk = close - stop
                target = close + rr_target * risk
                confluence = 0.0
                evidence = [
                    f"liquidity sweep below {sweep_level:.2f}",
                    f"bullish displacement/BOS in last {struct_lookback} bars",
                    f"range position {range_position:.2f} (discount)",
                ]
                if bull_zone:
                    if self._near(close, bull_zone, atr):
                        confluence += 0.12
                        evidence.append(
                            f"price inside bull zone {bull_zone[0]:.2f}-{bull_zone[1]:.2f}")
                    else:
                        evidence.append(
                            f"bull zone {bull_zone[0]:.2f}-{bull_zone[1]:.2f} nearby")
                conf = 0.44 + 0.14 * (1.0 - range_position) + confluence
                conf += 0.10 * min(1.0, abs(float(row.get("body_atr", 0.0) or 0.0)) / 1.5)
                sig = ctx.make_signal(
                    self.name, 1, close, stop, target, self._clamp_confidence(conf),
                    evidence=evidence,
                    invalidators=["close below the sweep low",
                                  "bearish CHoCH before entry"],
                    regime="smc", expires_bars=3, min_rr=min_rr,
                )
                if sig:
                    return [sig]

        # ----------------------------------------------------------- short setup
        swept_high = bool(recent["sweep_high"].any()) if "sweep_high" in recent else False
        if swept_high and self._displacement(structure, -1) and range_position >= 1.0 - max_pos:
            sweep_level = self._recent_level(recent, "sweep_high", "sweep_high_level", "high")
            zone_top = bear_zone[1] if bear_zone else sweep_level
            stop = max(sweep_level, zone_top) + buffer
            if stop > close:
                risk = stop - close
                target = close - rr_target * risk
                confluence = 0.0
                evidence = [
                    f"liquidity sweep above {sweep_level:.2f}",
                    f"bearish displacement/BOS in last {struct_lookback} bars",
                    f"range position {range_position:.2f} (premium)",
                ]
                if bear_zone:
                    if self._near(close, bear_zone, atr):
                        confluence += 0.12
                        evidence.append(
                            f"price inside bear zone {bear_zone[0]:.2f}-{bear_zone[1]:.2f}")
                    else:
                        evidence.append(
                            f"bear zone {bear_zone[0]:.2f}-{bear_zone[1]:.2f} nearby")
                conf = 0.44 + 0.14 * range_position + confluence
                conf += 0.10 * min(1.0, abs(float(row.get("body_atr", 0.0) or 0.0)) / 1.5)
                sig = ctx.make_signal(
                    self.name, -1, close, stop, target, self._clamp_confidence(conf),
                    evidence=evidence,
                    invalidators=["close above the sweep high",
                                  "bullish CHoCH before entry"],
                    regime="smc", expires_bars=3, min_rr=min_rr,
                )
                if sig:
                    return [sig]
        return []

    def should_exit(self, position: PositionView, ctx: StrategyContext) -> ExitDecision:
        row = ctx.row
        close = float(row["close"])
        if position.direction > 0:
            if bool(row.get("choch_down", False)) or bool(row.get("bos_down", False)):
                return ExitDecision("exit", "bearish structure break")
            if _finite(row.get("range_mid")) and close < float(row["range_mid"]) \
                    and position.bars_held >= 10:
                return ExitDecision("exit", "lost swing mid")
        else:
            if bool(row.get("choch_up", False)) or bool(row.get("bos_up", False)):
                return ExitDecision("exit", "bullish structure break")
            if _finite(row.get("range_mid")) and close > float(row["range_mid"]) \
                    and position.bars_held >= 10:
                return ExitDecision("exit", "lost swing mid")
        return ExitDecision()
