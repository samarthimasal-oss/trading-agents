"""Shared data models used across the agents and execution layer."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Candle:
    time: float          # bar open time (unix seconds)
    open: float
    high: float
    low: float
    close: float
    tick_volume: float


@dataclass
class MarketContext:
    """Everything the agents need to see about the current market."""
    symbol: str
    timeframe: str          # human label, e.g. "M15"
    bars: list[Candle] = field(default_factory=list)
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    rsi: Optional[float] = None
    atr: Optional[float] = None
    bid: float = 0.0
    ask: float = 0.0
    # spread in points (MT5 points)
    spread_points: float = 0.0
    digits: int = 5
    point: float = 0.00001


@dataclass
class AccountSnapshot:
    login: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    profit: float = 0.0            # floating P/L of all positions
    daily_pnl: float = 0.0         # P/L of bot-closed trades today + floating
    currency: str = "USD"


@dataclass
class Signal:
    """One quantitative agent's opinion about the market."""
    name: str
    direction: int          # +1 long, -1 short, 0 flat
    weight: float           # agent weight in consensus
    confidence: float       # 0..1
    reason: str


@dataclass
class Consensus:
    direction: int = 0
    strength: float = 0.0   # -1..1 (signed, weighted)
    confidence: float = 0.0 # 0..1
    signals: list[Signal] = field(default_factory=list)


@dataclass
class LlmReview:
    approved: bool = True
    action: str = "approve"     # approve | veto | resize
    size_multiplier: float = 1.0
    comment: str = ""
    used_llm: bool = False
    error: str = ""


@dataclass
class TradePlan:
    """Final decision of the pipeline, consumed by the executor."""
    symbol: str
    action: str = "none"        # none | buy | sell | close
    direction: int = 0
    volume: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    atr: float = 0.0
    entry_hint: float = 0.0
    consensus: Consensus = field(default_factory=Consensus)
    review: LlmReview = field(default_factory=LlmReview)
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    def summary(self) -> str:
        return (
            f"{self.action.upper()} {self.symbol} vol={self.volume} sl={self.sl} "
            f"tp={self.tp} | consensus d={self.consensus.direction} s={self.consensus.strength:+.2f} "
            f"c={self.consensus.confidence:.2f} | llm={self.review.action}"
            f"{(' (' + self.review.error + ')') if self.review.error else ''} | {self.reason}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["summary"] = self.summary()
        return d
