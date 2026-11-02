"""Five independent strategy engines, arbitration and research replay."""
from strategies.arbiter import ArbitrationResult, RejectedSignal, StrategyArbiter
from strategies.base import (
    BaseStrategy,
    ExitDecision,
    PositionView,
    StrategyContext,
    StrategySignal,
)
from strategies.registry import (
    DEFAULT_STRATEGY_CONFIG,
    DEFAULT_WEIGHTS,
    STRATEGY_CLASSES,
    build_strategies,
    engine_weights,
    strategy_config,
)

__all__ = [
    "ArbitrationResult",
    "BaseStrategy",
    "DEFAULT_STRATEGY_CONFIG",
    "DEFAULT_WEIGHTS",
    "ExitDecision",
    "PositionView",
    "RejectedSignal",
    "STRATEGY_CLASSES",
    "StrategyArbiter",
    "StrategyContext",
    "StrategySignal",
    "build_strategies",
    "engine_weights",
    "strategy_config",
]
