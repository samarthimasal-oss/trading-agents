"""Market data contracts: closed bars, validation, caching and retries."""
from __future__ import annotations

import pandas as pd
import pytest

from core.models import Candle
from market.data_engine import (
    BarSeries,
    MarketDataEngine,
    MarketDataError,
    epoch_seconds,
    normalize_epoch,
    timeframe_seconds,
)


def _frame(n: int = 20, start: float = 1_700_000_000.0, step: float = 900.0):
    rows = []
    price = 2000.0
    for i in range(n):
        price += 0.5
        rows.append({
            "time": start + i * step,
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.2,
            "tick_volume": 100 + i,
            "spread": 0.2,
        })
    return pd.DataFrame(rows)


def test_timeframe_seconds():
    assert timeframe_seconds("M15") == 900
    assert timeframe_seconds("H1") == 3600
    assert timeframe_seconds("D1") == 86400
    with pytest.raises(ValueError):
        timeframe_seconds("M7")


def test_epoch_seconds_survives_second_resolution():
    values = pd.Series(pd.to_datetime(
        ["2026-11-02T15:30:00Z", "2026-11-02T15:45:00Z"], utc=True
    ).astype("datetime64[s, UTC]"))
    assert epoch_seconds(values).tolist() == [1793633400, 1793634300]


def test_normalize_epoch_detects_numeric_units():
    seconds = 1793633400
    expected = [seconds, seconds + 900]
    assert normalize_epoch(pd.Series([seconds, seconds + 900])).tolist() == expected
    assert normalize_epoch(pd.Series([seconds * 1_000, (seconds + 900) * 1_000])).tolist() == expected
    assert normalize_epoch(
        pd.Series([seconds * 1_000_000, (seconds + 900) * 1_000_000])).tolist() == expected
    assert normalize_epoch(
        pd.Series([seconds * 1_000_000_000, (seconds + 900) * 1_000_000_000])).tolist() == expected


def test_normalize_epoch_parses_datetime_columns():
    values = pd.Series(pd.to_datetime(
        ["2026-11-02T15:30:00Z", "2026-11-02T15:45:00Z"], utc=True
    ))
    assert normalize_epoch(values).tolist() == [1793633400, 1793634300]


def test_from_dataframe_is_sorted_and_closed():
    df = _frame(10)
    series = BarSeries.from_dataframe(df.iloc[::-1], "XAUUSD", "M15")
    assert len(series) == 10
    assert series.bars[0].open_time < series.bars[-1].open_time
    assert all(b.is_closed for b in series.bars)
    assert series.last_closed().close > 0
    frame = series.to_frame()
    assert len(frame) == 10
    assert frame["time"].is_monotonic_increasing
    assert "session" not in frame.columns


def test_forming_bar_is_not_closed():
    df = _frame(3)
    now = float(df["time"].iloc[-1]) + 1.0
    series = BarSeries.from_dataframe(
        df, "XAUUSD", "M15", now=now, assume_closed=False)
    assert not series.bars[-1].is_closed
    assert len(series.closed_only()) == 2
    assert series.last_closed().open_time == float(df["time"].iloc[1])

    # once the close_time has passed the same bar becomes usable
    series2 = BarSeries.from_dataframe(
        df, "XAUUSD", "M15", now=float(df["time"].iloc[-1]) + 900.0,
        assume_closed=False)
    assert all(b.is_closed for b in series2.bars)


def test_duplicate_timestamps_are_dropped_and_reported():
    df = pd.concat([_frame(5), _frame(1)], ignore_index=True)
    series = BarSeries.from_dataframe(df, "XAUUSD", "M15")
    assert len(series) == 5
    assert any("duplicate" in issue for issue in series.issues)


def test_invalid_ohlc_is_fatal():
    df = _frame(5)
    df.loc[2, "high"] = df.loc[2, "low"] - 1.0
    series = BarSeries.from_dataframe(df, "XAUUSD", "M15")
    assert series.fatal_issues()
    assert any("high < low" in issue for issue in series.fatal_issues())


def test_gap_is_a_warning_not_a_fatal():
    df = _frame(4)
    df.loc[3, "time"] = df.loc[3, "time"] + 10 * 900
    series = BarSeries.from_dataframe(df, "XAUUSD", "M15")
    assert not series.fatal_issues()
    assert any("gap" in issue for issue in series.warning_issues())


class _FakeClient:
    def __init__(self, candles, failures=0):
        self.candles = candles
        self.failures = failures
        self.calls = 0

    def copy_bars(self, symbol, timeframe, count):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("feed unavailable")
        return self.candles[-int(count):]


def _candles(n=30):
    df = _frame(n)
    return [Candle(row.time, row.open, row.high, row.low, row.close,
                   row.tick_volume) for row in df.itertuples(index=False)]


def test_engine_caches_and_returns_closed_bars():
    client = _FakeClient(_candles(30))
    engine = MarketDataEngine(client, {"cache_seconds": 60, "retries": 1},
                              now_fn=lambda: 1_700_100_000.0)
    first = engine.get_bars("XAUUSD", "M15", 30)
    second = engine.get_bars("XAUUSD", "M15", 30)
    assert len(first) == len(second)
    assert client.calls == 1
    assert all(b.is_closed for b in first.bars)


def test_engine_retries_transient_failures():
    client = _FakeClient(_candles(20), failures=2)
    engine = MarketDataEngine(
        client, {"cache_seconds": 0, "retries": 3, "retry_sleep_seconds": 0},
        now_fn=lambda: 1_700_100_000.0)
    series = engine.get_bars("XAUUSD", "M15", 20)
    assert len(series) == 20
    assert client.calls == 3


def test_engine_raises_after_retries_exhausted():
    client = _FakeClient(_candles(20), failures=10)
    engine = MarketDataEngine(
        client, {"retries": 2, "retry_sleep_seconds": 0},
        now_fn=lambda: 1_700_100_000.0)
    with pytest.raises(MarketDataError):
        engine.get_bars("XAUUSD", "M15", 20)


def test_engine_rejects_structurally_invalid_bars():
    candles = _candles(10)
    candles[3].high = candles[3].low - 1.0
    engine = MarketDataEngine(
        _FakeClient(candles), {"retries": 1},
        now_fn=lambda: 1_700_100_000.0)
    with pytest.raises(MarketDataError):
        engine.get_bars("XAUUSD", "M15", 10)
