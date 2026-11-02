"""Strategy contract shared by all five engines.

A strategy returns a *proposal* (:class:`StrategySignal`), never an order.  The
arbiter ranks proposals and the risk governor decides whether anything is
executed.  Every engine is deterministic, works on closed bars only, and is
independent of the broker.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

import pandas as pd

from market.data_engine import timeframe_seconds

log = logging.getLogger("strategies")


@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    symbol: str
    timeframe: str
    direction: int
    entry: float
    stop: float
    target: float
    rr: float
    confidence: float
    expires_at: float
    setup_id: str
    evidence: tuple[str, ...] = ()
    invalidators: tuple[str, ...] = ()
    regime: str = ""
    session: str = ""
    bar_time: float = 0.0

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> float:
        return abs(self.target - self.entry)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExitDecision:
    action: str = "hold"          # hold | exit | trail
    reason: str = ""
    new_stop: Optional[float] = None


@dataclass(frozen=True)
class PositionView:
    strategy: str
    symbol: str
    direction: int
    entry: float
    stop: float
    target: float
    opened_at: float
    bars_held: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0


@dataclass
class StrategyContext:
    """Everything a strategy may see at one closed bar."""

    symbol: str
    timeframe: str
    frame: pd.DataFrame
    point: float = 0.01
    digits: int = 2
    spread_points: float = 0.0
    index: int = -1
    config: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.index < 0:
            self.index = len(self.frame) + self.index
        if self.frame is None or self.frame.empty:
            raise ValueError("strategy context requires a non-empty frame")
        if not (0 <= self.index < len(self.frame)):
            raise IndexError("strategy context index out of range")

    @property
    def row(self) -> pd.Series:
        return self.frame.iloc[self.index]

    @property
    def prev(self) -> pd.Series:
        return self.frame.iloc[max(0, self.index - 1)]

    @property
    def close(self) -> float:
        return float(self.row["close"])

    @property
    def atr(self) -> float:
        value = float(self.row.get("atr", 0.0) or 0.0)
        return value if math.isfinite(value) and value > 0 else 0.0

    @property
    def session(self) -> str:
        return str(self.row.get("session", ""))

    @property
    def bar_time(self) -> float:
        return float(self.row.get("time", 0.0))

    @property
    def spread_price(self) -> float:
        return float(self.spread_points) * float(self.point)

    def recent(self, n: int) -> pd.DataFrame:
        start = max(0, self.index - max(1, int(n)) + 1)
        return self.frame.iloc[start:self.index + 1]

    def session_allowed(self, sessions) -> bool:
        return not sessions or self.session in set(sessions)

    def spread_ok(self, max_spread_atr: float) -> bool:
        atr = self.atr
        if atr <= 0:
            return False
        if max_spread_atr <= 0:
            return True
        return self.spread_price <= max_spread_atr * atr

    def make_signal(
        self,
        strategy: str,
        direction: int,
        entry: float,
        stop: float,
        target: float,
        confidence: float,
        *,
        evidence=(),
        invalidators=(),
        regime: str = "",
        expires_bars: int = 4,
        min_rr: float = 1.0,
    ) -> Optional[StrategySignal]:
        try:
            entry = float(entry)
            stop = float(stop)
            target = float(target)
            confidence = float(confidence)
        except (TypeError, ValueError):
            return None
        if direction not in (-1, 1):
            return None
        if not all(math.isfinite(x) and x > 0 for x in (entry, stop, target)):
            return None
        if direction > 0 and not (stop < entry < target):
            return None
        if direction < 0 and not (target < entry < stop):
            return None
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0 or reward <= 0:
            return None
        rr = reward / risk
        if rr + 1e-9 < max(0.0, float(min_rr)):
            return None
        confidence = min(0.99, max(0.0, confidence))
        bar_time = self.bar_time
        setup_id = hashlib.sha1(
            f"{strategy}|{self.symbol}|{self.timeframe}|{bar_time:.0f}|"
            f"{direction}|{entry:.{max(0, self.digits)}f}"
            .encode("ascii")).hexdigest()[:16]
        return StrategySignal(
            strategy=strategy,
            symbol=self.symbol,
            timeframe=self.timeframe,
            direction=direction,
            entry=round(entry, self.digits),
            stop=round(stop, self.digits),
            target=round(target, self.digits),
            rr=round(rr, 4),
            confidence=round(confidence, 4),
            expires_at=bar_time + max(1, int(expires_bars)) * timeframe_seconds(self.timeframe),
            setup_id=setup_id,
            evidence=tuple(str(x) for x in evidence),
            invalidators=tuple(str(x) for x in invalidators),
            regime=regime,
            session=self.session,
            bar_time=bar_time,
        )


class BaseStrategy:
    """Base class for the five independent engines."""

    name = "base"
    defaults: dict = {
        "enabled": True,
        "min_bars": 60,
        "min_rr": 1.0,
        "max_spread_atr": 0.15,
        "sessions": ["london", "overlap", "newyork"],
        "max_bars_in_trade": 24,
    }

    def __init__(self, params: dict | None = None):
        self.params = {**self.defaults, **(params or {})}
        self.enabled = bool(self.params.get("enabled", True))

    # ------------------------------------------------------------------ helpers
    def _session_ok(self, ctx: StrategyContext) -> bool:
        return ctx.session_allowed(self.params.get("sessions") or [])

    def _spread_ok(self, ctx: StrategyContext) -> bool:
        return ctx.spread_ok(float(self.params.get("max_spread_atr", 0.0) or 0.0))

    def _ready(self, ctx: StrategyContext) -> bool:
        if not self.enabled:
            return False
        if len(ctx.frame) < int(self.params.get("min_bars", 50)):
            return False
        if ctx.atr <= 0:
            return False
        return self._session_ok(ctx) and self._spread_ok(ctx)

    def _clamp_confidence(self, value: float) -> float:
        return min(0.99, max(0.0, float(value)))

    # ------------------------------------------------------------------ interface
    def evaluate(self, ctx: StrategyContext) -> list[StrategySignal]:
        raise NotImplementedError

    def should_exit(self, position: PositionView,
                    ctx: StrategyContext) -> ExitDecision:
        return ExitDecision("hold", "")
