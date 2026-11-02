"""Market data layer.

One clock, one bar model, explicit availability.  Everything downstream
(features, structure, strategies, replay) consumes :class:`BarSeries` or the
DataFrame produced by :meth:`BarSeries.to_frame`; nobody calls MT5 directly.

The core rule: a bar is usable only when ``available_at <= now`` and
``is_closed`` is true.  The last MT5 row is usually the forming candle and is
excluded from every decision path.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import pandas as pd

log = logging.getLogger("market_data")

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 5 * 60,
    "M15": 15 * 60,
    "M30": 30 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
    "W1": 7 * 24 * 60 * 60,
    "MN1": 30 * 24 * 60 * 60,
}


class MarketDataError(RuntimeError):
    """Raised when a feed cannot be fetched or fails structural validation."""


def timeframe_seconds(timeframe: str) -> int:
    key = str(timeframe).upper()
    if key not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return TIMEFRAME_SECONDS[key]


def epoch_seconds(values) -> pd.Series:
    """Convert datetimes to integer epoch seconds at any pandas resolution."""
    ts = pd.to_datetime(values, utc=True)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    return (ts - epoch).dt.total_seconds().round().astype("int64")


def normalize_epoch(values) -> pd.Series:
    """Normalise datetime, second, millisecond, microsecond or nanosecond input.

    Pandas preserves the resolution of datetime columns (``datetime64[s]`` is
    common for MT5), so blindly dividing ``astype('int64')`` by ``1e9`` can
    collapse every timestamp to 1970.  This helper detects the unit instead.
    """
    if pd.api.types.is_datetime64_any_dtype(values):
        return epoch_seconds(values)
    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.dropna().abs()
    median = float(finite.median()) if len(finite) else 0.0
    if median > 1e17:      # nanoseconds
        return (numeric / 1e9).round().astype("int64")
    if median > 1e14:      # microseconds
        return (numeric / 1e6).round().astype("int64")
    if median > 1e11:      # milliseconds
        return (numeric / 1e3).round().astype("int64")
    return numeric.round().astype("int64")


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar with explicit availability."""

    open_time: float
    close_time: float
    available_at: float
    open: float
    high: float
    low: float
    close: float
    tick_volume: float = 0.0
    spread: float = 0.0
    is_closed: bool = True

    def as_dict(self) -> dict:
        return {
            "time": self.open_time,
            "close_time": self.close_time,
            "available_at": self.available_at,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "tick_volume": self.tick_volume,
            "spread": self.spread,
            "is_closed": self.is_closed,
        }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


@dataclass
class BarSeries:
    """An ordered set of bars for one symbol/timeframe."""

    symbol: str
    timeframe: str
    bars: list[Bar] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self):
        return iter(self.bars)

    def __getitem__(self, item):
        return self.bars[item]

    @property
    def seconds(self) -> int:
        return timeframe_seconds(self.timeframe)

    def closed_only(self) -> "BarSeries":
        return BarSeries(self.symbol, self.timeframe,
                         [b for b in self.bars if b.is_closed], list(self.issues))

    def last_closed(self) -> Optional[Bar]:
        for bar in reversed(self.bars):
            if bar.is_closed:
                return bar
        return None

    def to_frame(self, *, closed_only: bool = True) -> pd.DataFrame:
        rows = [b.as_dict() for b in self.bars if (b.is_closed or not closed_only)]
        if not rows:
            return pd.DataFrame(columns=[
                "time", "close_time", "available_at", "open", "high", "low",
                "close", "tick_volume", "spread", "is_closed",
            ])
        df = pd.DataFrame(rows).sort_values("time", kind="stable").reset_index(drop=True)
        df["dt_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["day_id"] = (df["time"] // 86_400).astype("int64")
        df["hour_utc"] = df["dt_utc"].dt.hour.astype("int64")
        return df

    # ------------------------------------------------------------------ build
    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        *,
        now: Optional[float] = None,
        assume_closed: bool = True,
        utc_offset_minutes: float = 0.0,
    ) -> "BarSeries":
        if df is None or len(df) == 0:
            return cls(symbol, timeframe, [], ["fatal: empty dataframe"])

        frame = df.copy()
        if "time" not in frame.columns:
            if "dt" in frame.columns:
                frame["time"] = epoch_seconds(frame["dt"])
            else:
                raise MarketDataError("dataframe must contain a 'time' column")
        if "tick_volume" not in frame.columns and "volume" in frame.columns:
            frame["tick_volume"] = frame["volume"]
        if "tick_volume" not in frame.columns:
            frame["tick_volume"] = 0.0
        if "spread" not in frame.columns:
            frame["spread"] = 0.0
        required = {"time", "open", "high", "low", "close"}
        missing = required - set(frame.columns)
        if missing:
            raise MarketDataError(f"dataframe missing columns: {sorted(missing)}")

        frame = frame[[
            "time", "open", "high", "low", "close", "tick_volume", "spread",
        ]].copy()
        frame["time"] = normalize_epoch(frame["time"]) + utc_offset_minutes * 60.0
        frame = frame.dropna(subset=["time"]).sort_values("time", kind="stable")

        issues: list[str] = []
        before = len(frame)
        frame = frame.drop_duplicates(subset=["time"], keep="last")
        if len(frame) < before:
            issues.append(f"warning: dropped {before - len(frame)} duplicate timestamp(s)")

        seconds = timeframe_seconds(timeframe)
        bars: list[Bar] = []
        for row in frame.itertuples(index=False):
            open_time = float(row.time)
            close_time = open_time + seconds
            available = close_time
            is_closed = bool(assume_closed or (now is not None and available <= now))
            bars.append(Bar(
                open_time=open_time,
                close_time=close_time,
                available_at=available,
                open=_as_float(row.open),
                high=_as_float(row.high),
                low=_as_float(row.low),
                close=_as_float(row.close),
                tick_volume=_as_float(row.tick_volume),
                spread=_as_float(row.spread),
                is_closed=is_closed,
            ))
        series = cls(symbol, timeframe, bars, issues)
        series.issues.extend(series.validate())
        return series

    @classmethod
    def from_candles(
        cls,
        candles: Iterable[Any],
        symbol: str,
        timeframe: str,
        *,
        now: Optional[float] = None,
        assume_closed: bool = False,
        utc_offset_minutes: float = 0.0,
    ) -> "BarSeries":
        rows = []
        for candle in candles or []:
            rows.append({
                "time": getattr(candle, "time", None),
                "open": getattr(candle, "open", None),
                "high": getattr(candle, "high", None),
                "low": getattr(candle, "low", None),
                "close": getattr(candle, "close", None),
                "tick_volume": getattr(candle, "tick_volume", 0.0),
                "spread": getattr(candle, "spread", 0.0),
            })
        return cls.from_dataframe(
            pd.DataFrame(rows), symbol, timeframe,
            now=now, assume_closed=assume_closed,
            utc_offset_minutes=utc_offset_minutes,
        )

    # --------------------------------------------------------------- validate
    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.bars:
            return ["fatal: no bars"]
        prev_time: Optional[float] = None
        for idx, bar in enumerate(self.bars):
            label = f"bar[{idx}] t={bar.open_time:.0f}"
            prices = (bar.open, bar.high, bar.low, bar.close)
            if any((not math.isfinite(p)) or p <= 0 for p in prices):
                issues.append(f"fatal: {label}: non-positive/non-finite OHLC")
                continue
            if bar.high < bar.low:
                issues.append(f"fatal: {label}: high < low")
            if bar.high + 1e-12 < max(bar.open, bar.close):
                issues.append(f"fatal: {label}: high below open/close")
            if bar.low - 1e-12 > min(bar.open, bar.close):
                issues.append(f"fatal: {label}: low above open/close")
            if bar.tick_volume < 0:
                issues.append(f"fatal: {label}: negative volume")
            if bar.spread < 0:
                issues.append(f"fatal: {label}: negative spread")
            if prev_time is not None:
                delta = bar.open_time - prev_time
                if delta <= 0:
                    issues.append(f"fatal: {label}: timestamps not strictly increasing")
                elif delta > self.seconds * 1.5:
                    issues.append(
                        f"warning: {label}: gap {delta:.0f}s > {self.seconds}s")
            prev_time = bar.open_time
        return issues

    def fatal_issues(self) -> list[str]:
        return [i for i in self.issues if i.startswith("fatal:")]

    def warning_issues(self) -> list[str]:
        return [i for i in self.issues if i.startswith("warning:")]


class MarketDataEngine:
    """Fetch, validate and cache closed bars.

    The client is duck-typed: anything with ``copy_bars(symbol, timeframe,
    count)`` returning objects with ``time/open/high/low/close`` works.
    """

    def __init__(self, client, config: Optional[dict] = None, *,
                 now_fn=time.time):
        self.client = client
        self.config = config or {}
        self.now_fn = now_fn
        self.cache_seconds = float(self.config.get("cache_seconds", 5))
        self.retries = max(1, int(self.config.get("retries", 3)))
        self.retry_sleep = max(0.0, float(self.config.get("retry_sleep_seconds", 0.5)))
        self.utc_offset_minutes = float(self.config.get("server_utc_offset_minutes", 0))
        self._cache: dict[tuple[str, str], tuple[float, BarSeries]] = {}

    def clear_cache(self, symbol: Optional[str] = None,
                    timeframe: Optional[str] = None) -> None:
        if symbol is None and timeframe is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if (symbol is None or key[0] == symbol) and \
                    (timeframe is None or key[1] == timeframe):
                self._cache.pop(key, None)

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        count: int = 300,
        *,
        refresh: bool = False,
        include_forming: bool = False,
    ) -> BarSeries:
        key = (symbol, timeframe)
        now = float(self.now_fn())
        if not refresh and key in self._cache:
            fetched_at, cached = self._cache[key]
            if now - fetched_at <= self.cache_seconds:
                return cached if include_forming else cached.closed_only()

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                candles = self.client.copy_bars(symbol, timeframe, int(count))
                if not candles:
                    raise MarketDataError(f"no bars returned for {symbol} {timeframe}")
                series = BarSeries.from_candles(
                    candles, symbol, timeframe,
                    now=now, assume_closed=False,
                    utc_offset_minutes=self.utc_offset_minutes,
                )
                fatal = series.fatal_issues()
                if fatal:
                    raise MarketDataError("; ".join(fatal[:3]))
                for warning in series.warning_issues():
                    log.warning("%s %s: %s", symbol, timeframe, warning)
                self._cache[key] = (now, series)
                return series if include_forming else series.closed_only()
            except Exception as exc:  # noqa: BLE001 - retry every feed failure
                last_error = exc
                log.warning("market data fetch failed (%s %s, attempt %d/%d): %s",
                            symbol, timeframe, attempt, self.retries, exc)
                if attempt < self.retries and self.retry_sleep > 0:
                    time.sleep(self.retry_sleep * attempt)
        raise MarketDataError(
            f"could not fetch {symbol} {timeframe} after {self.retries} attempts: "
            f"{last_error}")

    def get_frame(
        self,
        symbol: str,
        timeframe: str,
        count: int = 300,
        *,
        refresh: bool = False,
    ) -> pd.DataFrame:
        return self.get_bars(symbol, timeframe, count, refresh=refresh).to_frame()
