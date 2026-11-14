"""Market data and deterministic feature snapshot.

No forecast or feature is invented.  A missing candle series yields an
``available: false`` block with a reason; an old Kronos cache is labelled
``stale`` and never presented as a live forecast.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.errors import APIError


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    seed = sum(values[:period]) / period
    result = seed
    k = 2.0 / (period + 1.0)
    for value in values[period:]:
        result = value * k + result * (1.0 - k)
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    ranges: list[float] = []
    for index in range(1, len(candles)):
        high = float(candles[index]["high"])
        low = float(candles[index]["low"])
        previous = float(candles[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    if len(ranges) < period:
        return None
    result = sum(ranges[:period]) / period
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result


def _session(now: datetime) -> str:
    hour = now.hour
    if 0 <= hour < 7:
        return "tokyo"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london/new-york overlap"
    if 16 <= hour < 21:
        return "new-york"
    return "late-session"


class MarketService:
    def __init__(self, mt5: Any, root: str | Path,
                 config_loader: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self.mt5 = mt5
        self.root = Path(root)
        self.config_loader = config_loader or (lambda: {})
        self.clock = clock

    # ------------------------------------------------------------------- quote
    def quote(self, symbol: str) -> dict:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise APIError(422, "validation_error", "symbol is required")
        return self.mt5.quote(symbol)

    def candles(self, symbol: str, timeframe: str = "M15", bars: int = 120) -> dict:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise APIError(422, "validation_error", "symbol is required")
        timeframe = (timeframe or "M15").upper()
        if timeframe not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}:
            raise APIError(422, "validation_error", f"unsupported timeframe {timeframe!r}")
        rows = self.mt5.candles(symbol, timeframe, bars)
        return {"symbol": symbol, "timeframe": timeframe, "count": len(rows),
                "candles": rows}

    # ---------------------------------------------------------------- overview
    def overview(self, symbol: str) -> dict:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise APIError(422, "validation_error", "symbol is required")
        cfg = self.config_loader() or {}
        quote = self.quote(symbol)
        bars: list[dict] = []
        candle_error = None
        try:
            bars = self.mt5.candles(symbol, "M15", 240)
        except Exception as exc:                        # noqa: BLE001
            candle_error = str(exc)

        features = self._features(bars) if len(bars) >= 30 else {
            "available": False,
            "reason": candle_error or "fewer than 30 closed M15 bars",
        }
        now = datetime.now(timezone.utc)
        risk = cfg.get("risk") or {}
        symbol_spreads = risk.get("max_spread_points_by_symbol") or {}
        max_spread = float(symbol_spreads.get(symbol)
                           or risk.get("max_spread_points") or 0.0)
        spread = float(quote.get("spread_points") or 0.0)
        kronos = self._kronos(symbol, cfg)
        news = self._news(cfg)

        reasons: list[str] = []
        if not quote.get("bid") or not quote.get("ask"):
            reasons.append("no valid two-sided quote")
        if quote.get("ask") and quote.get("bid") and quote["ask"] < quote["bid"]:
            reasons.append("crossed quote")
        if max_spread and spread > max_spread:
            reasons.append(f"spread {spread:.1f} exceeds limit {max_spread:.1f}")
        if not features.get("available"):
            reasons.append("feature window unavailable")
        if news.get("blocked"):
            reasons.append("high-impact news window")
        if kronos.get("stale"):
            reasons.append("forecast is stale")

        return {
            "symbol": symbol,
            "ts": self.clock(),
            "quote": quote,
            "features": features,
            "session": _session(now),
            "kronos": kronos,
            "news": news,
            "spread": {
                "points": spread,
                "limit_points": max_spread or None,
                "ok": not max_spread or spread <= max_spread,
            },
            "tradable": not reasons,
            "reasons": reasons,
            "candles": bars[-120:],
            "candle_error": candle_error,
        }

    # ---------------------------------------------------------------- features
    @staticmethod
    def _features(bars: list[dict]) -> dict:
        closes = [float(bar["close"]) for bar in bars]
        ema_fast = _ema(closes, 20)
        ema_slow = _ema(closes, 50)
        rsi = _rsi(closes, 14)
        atr = _atr(bars, 14)
        recent = bars[-20:]
        range_high = max(float(bar["high"]) for bar in recent)
        range_low = min(float(bar["low"]) for bar in recent)
        last = closes[-1]
        if ema_fast is not None and ema_slow is not None and atr:
            if ema_fast > ema_slow + atr * 0.15:
                regime = "up"
            elif ema_fast < ema_slow - atr * 0.15:
                regime = "down"
            else:
                regime = "range"
        else:
            regime = "unknown"
        if last > range_high:
            structure = "above recent range"
        elif last < range_low:
            structure = "below recent range"
        else:
            structure = "inside recent range"
        return {
            "available": True,
            "last_close": last,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi,
            "atr": atr,
            "range_high": range_high,
            "range_low": range_low,
            "structure": structure,
            "regime": regime,
            "bars": len(bars),
        }

    # ------------------------------------------------------------------ kronos
    def _kronos(self, symbol: str, cfg: dict) -> dict:
        path = self.root / "state" / "kronos_tf_cache.json"
        if not path.exists():
            return {"available": False, "reason": "no Kronos cache on disk"}
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": f"Kronos cache unreadable: {exc}"}
        if not isinstance(cache, dict):
            return {"available": False, "reason": "Kronos cache has an unknown shape"}
        matches = [(key, value) for key, value in cache.items()
                   if str(key).upper().startswith(f"{symbol}|")]
        if not matches:
            return {"available": False, "reason": f"no Kronos forecast cached for {symbol}"}
        key, entry = max(matches, key=lambda item: float(
            (item[1] or {}).get("last_ts") or 0.0))
        last_ts = float((entry or {}).get("last_ts") or 0.0)
        age = max(0.0, self.clock() - last_ts) if last_ts else None
        timeframe = str(key).split("|", 1)[1] if "|" in str(key) else "?"
        seconds = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(
            timeframe, 3600)
        stale = age is None or age > seconds * 4
        payload = (entry or {}).get("payload") or {}
        candles = payload.get("candles") or []
        return {
            "available": True,
            "stale": stale,
            "age_seconds": age,
            "timeframe": timeframe,
            "direction": payload.get("dir"),
            "move": payload.get("move"),
            "move_next": payload.get("move_next"),
            "path_high": payload.get("path_hi"),
            "path_low": payload.get("path_lo"),
            "ood": payload.get("ood"),
            "forecast_candles": candles[-8:],
            "source": str(path),
            "warning": ("forecast cache is older than 4 bars; do not treat as live"
                        if stale else None),
        }

    # -------------------------------------------------------------------- news
    def _news(self, cfg: dict) -> dict:
        news_cfg = cfg.get("news") or {}
        if not news_cfg.get("enabled", True):
            return {"available": True, "enabled": False, "blocked": False,
                    "events": []}
        path = self.root / "state" / "news_calendar.json"
        if not path.exists():
            return {"available": False, "blocked": False, "events": [],
                    "reason": "no news calendar on disk"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"available": False, "blocked": False, "events": [],
                    "reason": f"news calendar unreadable: {exc}"}
        events = data.get("events") or []
        now = self.clock()
        before = float(news_cfg.get("window_before_min") or 10) * 60
        after = float(news_cfg.get("window_after_min") or 10) * 60
        min_impact = str(news_cfg.get("min_impact") or "High").lower()
        rank = {"low": 0, "medium": 1, "high": 2, "holiday": 0}
        threshold = rank.get(min_impact, 1)
        window = []
        blocked = False
        for event in events:
            event_ts = float(event.get("ts") or 0)
            if not event_ts:
                continue
            impact = str(event.get("impact") or "").lower()
            if rank.get(impact, 0) < threshold:
                continue
            if now - after <= event_ts <= now + before:
                blocked = True
                window.append({**event, "blocking": True})
        upcoming = [event for event in events
                    if float(event.get("ts") or 0) > now][:10]
        calendar_ts = float(data.get("ts") or 0)
        stale = calendar_ts and now - calendar_ts > 86400
        return {
            "available": True,
            "stale": bool(stale),
            "calendar_age_seconds": (now - calendar_ts) if calendar_ts else None,
            "blocked": blocked,
            "events": window,
            "upcoming": upcoming,
            "warning": ("news calendar is older than 24h; blocking is paused"
                        if stale else None),
        }


__all__ = ["MarketService"]
