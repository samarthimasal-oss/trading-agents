"""Deterministic arbitration between independent strategy proposals.

The arbiter does not create signals and does not size positions.  It enforces
portfolio-level selection rules:

  * drop expired, disabled, low-confidence and low-R proposals;
  * keep at most one proposal per strategy per bar;
  * de-duplicate by setup id;
  * optionally forbid simultaneous long and short exposure;
  * cap the number of proposals per bar.

Every dropped proposal is returned with a reason so the dashboard and replay
report can explain *why* a setup was not taken.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from strategies.base import StrategySignal
from strategies.registry import DEFAULT_STRATEGY_CONFIG, engine_weights, strategy_config


@dataclass(frozen=True)
class RejectedSignal:
    signal: StrategySignal
    reason: str


@dataclass
class ArbitrationResult:
    selected: list[StrategySignal] = field(default_factory=list)
    rejected: list[RejectedSignal] = field(default_factory=list)
    considered: int = 0

    @property
    def rejections_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


class StrategyArbiter:
    def __init__(self, config: dict | None = None):
        section = strategy_config(config)
        self.config = section
        self.weights = engine_weights(config)
        self.min_confidence = float(section.get("min_confidence",
                                                DEFAULT_STRATEGY_CONFIG["min_confidence"]))
        self.min_rr = float(section.get("min_rr", DEFAULT_STRATEGY_CONFIG["min_rr"]))
        self.max_signals = max(1, int(section.get("max_signals_per_bar",
                                                  DEFAULT_STRATEGY_CONFIG["max_signals_per_bar"])))
        self.allow_opposite = bool(section.get("allow_opposite_directions", False))
        self.master_on = bool(section.get("enabled", True))

    def score(self, signal: StrategySignal) -> float:
        weight = float(self.weights.get(signal.strategy, 1.0))
        rr_bonus = 1.0 + 0.05 * min(4.0, max(0.0, float(signal.rr)))
        return float(signal.confidence) * weight * rr_bonus

    def arbitrate(self, signals, now: float) -> ArbitrationResult:
        signal_list = list(signals)
        result = ArbitrationResult(considered=len(signal_list))
        if not self.master_on:
            for signal in signal_list:
                result.rejected.append(RejectedSignal(signal, "strategy master switch off"))
            return result

        candidates: list[StrategySignal] = []
        seen_setups: set[str] = set()
        seen_strategies: set[str] = set()

        for signal in sorted(signal_list, key=lambda s: (-self.score(s), s.setup_id)):
            if not self._signal_ok(signal, now, result):
                continue
            if signal.setup_id in seen_setups:
                result.rejected.append(RejectedSignal(signal, "duplicate setup id"))
                continue
            if signal.strategy in seen_strategies:
                result.rejected.append(RejectedSignal(signal, "strategy already proposed"))
                continue
            seen_setups.add(signal.setup_id)
            seen_strategies.add(signal.strategy)
            candidates.append(signal)

        if not self.allow_opposite and candidates:
            chosen_direction = candidates[0].direction
            allowed = [s for s in candidates if s.direction == chosen_direction]
            for signal in candidates:
                if signal.direction != chosen_direction:
                    result.rejected.append(
                        RejectedSignal(signal, "opposite direction to top-ranked signal"))
            candidates = allowed

        for index, signal in enumerate(candidates):
            if index < self.max_signals:
                result.selected.append(signal)
            else:
                result.rejected.append(RejectedSignal(signal, "per-bar signal cap"))
        return result

    def _signal_ok(self, signal: StrategySignal, now: float,
                   result: ArbitrationResult) -> bool:
        if signal.strategy not in self.weights:
            result.rejected.append(RejectedSignal(signal, "unknown strategy"))
            return False
        if not self.master_on:
            result.rejected.append(RejectedSignal(signal, "strategy master switch off"))
            return False
        if signal.expires_at and float(now) > float(signal.expires_at):
            result.rejected.append(RejectedSignal(signal, "expired"))
            return False
        if signal.confidence < self.min_confidence:
            result.rejected.append(
                RejectedSignal(signal, f"confidence {signal.confidence:.2f} < "
                                       f"{self.min_confidence:.2f}"))
            return False
        if signal.rr < self.min_rr:
            result.rejected.append(
                RejectedSignal(signal, f"rr {signal.rr:.2f} < {self.min_rr:.2f}"))
            return False
        if signal.direction not in (-1, 1) or signal.risk <= 0:
            result.rejected.append(RejectedSignal(signal, "invalid direction/risk"))
            return False
        return True


__all__ = ["ArbitrationResult", "RejectedSignal", "StrategyArbiter"]
