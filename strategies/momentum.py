"""Momentum-continuation engine.

MACD/ROC acceleration with RSI in the healthy middle of its range and a
volume expansion.  It deliberately avoids the exhausted tail of a move: an
entry more than ``max_extension_atr`` above EMA20 is rejected.
"""
from __future__ import annotations

import math

from strategies.base import BaseStrategy, ExitDecision, PositionView, StrategyContext


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class MomentumStrategy(BaseStrategy):
    name = "momentum"
    defaults = {
        **BaseStrategy.defaults,
        "rsi_min": 52.0,
        "rsi_max": 72.0,
        "adx_min": 18.0,
        "roc5_min": 0.0005,
        "vol_z_min": -0.2,
        "body_atr_min": 0.30,
        "atr_stop": 1.20,
        "rr_target": 2.0,
        "max_extension_atr": 2.20,
        "min_rr": 1.5,
        "max_spread_atr": 0.15,
        "min_bars": 80,
        "max_bars_in_trade": 24,
        "sessions": ["london", "overlap", "newyork"],
    }

    def _score(self, ctx: StrategyContext, direction: int, rsi: float,
               adx: float, roc: float, vol_z: float, extension: float) -> float:
        conf = 0.42
        conf += 0.16 * min(1.0, max(0.0, (adx - float(self.params["adx_min"])) / 25.0))
        conf += 0.14 * min(1.0, abs(roc) / max(1e-9, float(self.params["roc5_min"])))
        conf += 0.10 * min(1.0, max(0.0, vol_z) / 2.0)
        room = 1.0 - min(1.0, extension / max(1e-9, float(self.params["max_extension_atr"])))
        conf += 0.12 * room
        if direction > 0:
            conf += 0.06 * max(0.0, min(1.0, (rsi - float(self.params["rsi_min"])) / 10.0))
        else:
            conf += 0.06 * max(0.0, min(1.0, (float(self.params["rsi_max"]) - rsi) / 10.0))
        return self._clamp_confidence(conf)

    def evaluate(self, ctx: StrategyContext):
        if not self._ready(ctx):
            return []
        row = ctx.row
        atr = ctx.atr
        close = ctx.close
        try:
            ema20 = float(row["ema20"])
            ema50 = float(row["ema50"])
            rsi = float(row["rsi"])
            adx = float(row["adx"])
            roc = float(row["roc_5"])
            macd_hist = float(row["macd_hist"])
            macd_slope = float(row["macd_hist_slope"])
            vol_z = float(row["vol_z"]) if _finite(row.get("vol_z")) else 0.0
            body_atr = float(row["body_atr"]) if _finite(row.get("body_atr")) else 0.0
            open_ = float(row["open"])
        except (KeyError, TypeError, ValueError):
            return []
        if not all(math.isfinite(x) for x in
                    (ema20, ema50, rsi, adx, roc, macd_hist, macd_slope, open_)):
            return []

        extension = (close - ema20) / atr
        if extension > float(self.params["max_extension_atr"]):
            return []

        long_ok = (
            close > ema20 > ema50
            and macd_hist > 0 and macd_slope > 0
            and float(self.params["rsi_min"]) <= rsi <= float(self.params["rsi_max"])
            and roc >= float(self.params["roc5_min"])
            and adx >= float(self.params["adx_min"])
            and vol_z >= float(self.params["vol_z_min"])
            and body_atr >= float(self.params["body_atr_min"])
            and close > open_
        )
        if long_ok:
            recent_low = float(ctx.recent(5)["low"].min())
            stop = min(recent_low, ema20 - 0.35 * atr,
                       close - float(self.params["atr_stop"]) * atr)
            if stop < close:
                risk = close - stop
                target = close + float(self.params["rr_target"]) * risk
                conf = self._score(ctx, 1, rsi, adx, roc, vol_z, extension)
                sig = ctx.make_signal(
                    self.name, 1, close, stop, target, conf,
                    evidence=[
                        f"MACD hist {macd_hist:+.3f} rising",
                        f"RSI {rsi:.1f}, ADX {adx:.1f}",
                        f"ROC5 {roc:+.3%}, volume z {vol_z:+.2f}",
                        f"extension {extension:.2f} ATR",
                    ],
                    invalidators=["MACD histogram turns negative",
                                  "close below EMA20"],
                    regime="momentum", expires_bars=2,
                    min_rr=float(self.params["min_rr"]),
                )
                if sig:
                    return [sig]

        extension = (ema20 - close) / atr
        if extension > float(self.params["max_extension_atr"]):
            return []
        short_ok = (
            close < ema20 < ema50
            and macd_hist < 0 and macd_slope < 0
            and (100.0 - float(self.params["rsi_max"])) <= rsi <= (100.0 - float(self.params["rsi_min"]))
            and roc <= -float(self.params["roc5_min"])
            and adx >= float(self.params["adx_min"])
            and vol_z >= float(self.params["vol_z_min"])
            and body_atr >= float(self.params["body_atr_min"])
            and close < open_
        )
        if short_ok:
            recent_high = float(ctx.recent(5)["high"].max())
            stop = max(recent_high, ema20 + 0.35 * atr,
                       close + float(self.params["atr_stop"]) * atr)
            if stop > close:
                risk = stop - close
                target = close - float(self.params["rr_target"]) * risk
                conf = self._score(ctx, -1, rsi, adx, roc, vol_z, extension)
                sig = ctx.make_signal(
                    self.name, -1, close, stop, target, conf,
                    evidence=[
                        f"MACD hist {macd_hist:+.3f} falling",
                        f"RSI {rsi:.1f}, ADX {adx:.1f}",
                        f"ROC5 {roc:+.3%}, volume z {vol_z:+.2f}",
                        f"extension {extension:.2f} ATR",
                    ],
                    invalidators=["MACD histogram turns positive",
                                  "close above EMA20"],
                    regime="momentum", expires_bars=2,
                    min_rr=float(self.params["min_rr"]),
                )
                if sig:
                    return [sig]
        return []

    def should_exit(self, position: PositionView, ctx: StrategyContext) -> ExitDecision:
        row = ctx.row
        try:
            macd_hist = float(row["macd_hist"])
            close = float(row["close"])
            ema20 = float(row["ema20"])
        except (KeyError, TypeError, ValueError):
            return ExitDecision()
        if position.direction > 0:
            if macd_hist < 0 or close < ema20:
                return ExitDecision("exit", "momentum faded")
        else:
            if macd_hist > 0 or close > ema20:
                return ExitDecision("exit", "momentum faded")
        return ExitDecision()
