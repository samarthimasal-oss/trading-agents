"""JSON-safety of the MT5 adapter.

MetaTrader5 hands back numpy scalars from ``copy_rates_*`` and ticks.  FastAPI's
serializer refuses those, so the adapter must coerce every value it forwards.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from api.services.mt5_adapter import MT5Adapter


@dataclass
class FakeCandle:
    time: object
    open: object
    high: object
    low: object
    close: object
    tick_volume: object


class FakeClient:
    def resolve_symbol(self, symbol: str) -> str:
        return symbol

    def copy_bars(self, symbol: str, timeframe: str, bars: int):
        return [
            FakeCandle(np.int64(1794585000), np.float64(3859.1), np.float64(3861.2),
                       np.float64(3858.4), np.float64(3860.3), np.int64(120)),
            FakeCandle(np.int64(1794585900), np.float64(3860.3), np.float64(3862.0),
                       np.float64(3859.8), np.float64(3861.1), np.int64(88)),
        ]

    def prepare(self, symbol: str):
        return SimpleNamespace(point=0.01, digits=2)

    def tick(self, symbol: str):
        return SimpleNamespace(bid=np.float64(3859.9), ask=np.float64(3860.2),
                               time=np.int64(1794586000))

    def account(self):
        raise AssertionError("not needed")


def test_candles_and_quote_are_json_safe():
    adapter = MT5Adapter(FakeClient())

    candles = adapter.candles("XAUUSD", "M15", 2)
    quote = adapter.quote("XAUUSD")

    encoded = json.dumps({"candles": candles, "quote": quote})
    restored = json.loads(encoded)

    assert restored["quote"]["time"] == 1794586000
    assert restored["quote"]["spread_points"] == pytest.approx(30.0)
    assert restored["candles"][0] == {
        "time": 1794585000.0,
        "open": 3859.1,
        "high": 3861.2,
        "low": 3858.4,
        "close": 3860.3,
        "tick_volume": 120.0,
    }
    assert all(isinstance(candle["time"], float) for candle in restored["candles"])
