"""MT5 adapter that converts the trading client into JSON-safe values."""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any

from api.errors import APIError

log = logging.getLogger("api.mt5")


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class MT5Adapter:
    def __init__(self, client: Any, magic: int | None = None,
                 risk_cfg: dict | None = None,
                 comment: str = "TradingAgents"):
        self.client = client
        self.magic = int(magic or 0)
        self.risk_cfg = risk_cfg or {}
        self.comment = comment
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ health
    def health(self) -> dict:
        try:
            with self._lock:
                account = self.client.account()
            return {"ok": True, "detail": f"connected to login {_value(account, 'login', '?')}"}
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    # ----------------------------------------------------------------- account
    def account(self) -> dict:
        with self._lock:
            snapshot = self.client.account()
        if dataclasses.is_dataclass(snapshot):
            return dataclasses.asdict(snapshot)
        if isinstance(snapshot, dict):
            return dict(snapshot)
        return {field.name: getattr(snapshot, field.name)
                for field in dataclasses.fields(snapshot)}

    def daily_bot_profit(self) -> float:
        with self._lock:
            return float(self.client.daily_bot_profit(self.magic))

    # --------------------------------------------------------------- positions
    def positions(self, symbol: str | None = None,
                  magic: int | None = None) -> list[dict]:
        with self._lock:
            rows = self.client.positions(symbol=symbol,
                                         magic=self.magic if magic is None else magic)
        return [self._position(row) for row in rows]

    @staticmethod
    def _position(row: Any) -> dict:
        pos_type = int(_value(row, "type", -1))
        return {
            "ticket": int(_value(row, "ticket", 0)),
            "symbol": str(_value(row, "symbol", "")),
            "type": pos_type,
            "side": "buy" if pos_type == 0 else "sell" if pos_type == 1 else "unknown",
            "volume": float(_value(row, "volume", 0.0) or 0.0),
            "price_open": float(_value(row, "price_open", 0.0) or 0.0),
            "price_current": float(_value(row, "price_current", 0.0) or 0.0),
            "sl": float(_value(row, "sl", 0.0) or 0.0),
            "tp": float(_value(row, "tp", 0.0) or 0.0),
            "profit": float(_value(row, "profit", 0.0) or 0.0),
            "swap": float(_value(row, "swap", 0.0) or 0.0),
            "magic": int(_value(row, "magic", 0) or 0),
            "comment": str(_value(row, "comment", "") or ""),
            "time": _value(row, "time"),
        }

    def orders(self, symbol: str | None = None,
               magic: int | None = None) -> list[dict]:
        with self._lock:
            rows = self.client.orders(symbol=symbol,
                                      magic=self.magic if magic is None else magic)
        out = []
        for row in rows:
            order_type = int(_value(row, "type", -1))
            out.append({
                "ticket": int(_value(row, "ticket", 0)),
                "symbol": str(_value(row, "symbol", "")),
                "type": order_type,
                "side": "buy" if order_type in (0, 2) else "sell",
                "volume_current": float(_value(row, "volume_current", 0.0) or 0.0),
                "volume_initial": float(_value(row, "volume_initial", 0.0) or 0.0),
                "price_open": float(_value(row, "price_open", 0.0) or 0.0),
                "price_current": float(_value(row, "price_current", 0.0) or 0.0),
                "sl": float(_value(row, "sl", 0.0) or 0.0),
                "tp": float(_value(row, "tp", 0.0) or 0.0),
                "magic": int(_value(row, "magic", 0) or 0),
                "comment": str(_value(row, "comment", "") or ""),
                "time_setup": _value(row, "time_setup"),
            })
        return out

    # ------------------------------------------------------------------- quotes
    def quote(self, symbol: str) -> dict:
        with self._lock:
            resolved = self.client.resolve_symbol(symbol)
            info = self.client.prepare(resolved)
            tick = self.client.tick(resolved)
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        point = float(getattr(info, "point", 0.0) or 0.0)
        spread = (ask - bid) / point if point and bid and ask else 0.0
        return {
            "symbol": resolved,
            "requested_symbol": symbol,
            "bid": bid,
            "ask": ask,
            "digits": int(getattr(info, "digits", 0) or 0),
            "point": point,
            "spread_points": spread,
            "time": int(getattr(tick, "time", 0) or 0),
        }

    def candles(self, symbol: str, timeframe: str = "M15",
                bars: int = 120) -> list[dict]:
        timeframe = str(timeframe or "M15").upper()
        bars = max(1, min(int(bars), 1500))
        with self._lock:
            resolved = self.client.resolve_symbol(symbol)
            rows = self.client.copy_bars(resolved, timeframe, bars)
        # MT5 returns numpy scalars for OHLC fields.  FastAPI's serializer
        # refuses those, so every value is coerced to a plain Python number.
        out = []
        for row in rows:
            source = dataclasses.asdict(row) if dataclasses.is_dataclass(row) else dict(row)
            out.append({
                "time": float(source.get("time") or 0.0),
                "open": float(source.get("open") or 0.0),
                "high": float(source.get("high") or 0.0),
                "low": float(source.get("low") or 0.0),
                "close": float(source.get("close") or 0.0),
                "tick_volume": float(source.get("tick_volume") or 0.0),
            })
        return out

    # --------------------------------------------------------------- close all
    def close_all(self, magic: int | None = None) -> int:
        try:
            from execution.executor import Executor
        except Exception as exc:                        # noqa: BLE001
            raise APIError(503, "mt5_unavailable",
                           "the MT5 execution layer is unavailable",
                           str(exc)) from exc
        with self._lock:
            executor = Executor(self.client, int(self.magic if magic is None else magic),
                                self.risk_cfg, comment=self.comment)
            return int(executor.close_all())

    def shutdown(self) -> None:
        try:
            with self._lock:
                self.client.shutdown()
        except Exception:                               # noqa: BLE001
            log.debug("MT5 shutdown failed", exc_info=True)


class UnavailableMT5:
    """Safe stand-in when the MT5 package or terminal is unavailable."""

    def __init__(self, reason: str):
        self.reason = reason

    def _fail(self, *args, **kwargs):
        raise APIError(503, "mt5_unavailable", self.reason)

    health = account = positions = orders = quote = candles = _fail
    daily_bot_profit = close_all = _fail

    def shutdown(self) -> None:
        return None


__all__ = ["MT5Adapter", "UnavailableMT5"]
