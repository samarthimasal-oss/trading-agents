"""Walk-forward and untouched-holdout validation for the five engines.

This is research tooling, not a promotion mechanism.  It is deliberately
conservative about what it claims:

  * the strategy rules are **fixed** — no parameter fitting happens on the
    fold data, so a walk-forward here measures *temporal stability*, not
    learned generalisation;
  * folds are contiguous, non-overlapping and expanding (each test window
    keeps all history before it as context);
  * a trade belongs to the window in which it was **entered**; a position may
    resolve after the window boundary, but no decision ever sees future bars;
  * the final holdout is evaluated exactly once and is never used to choose a
    parameter, a variant or a strategy;
  * the Monte Carlo bootstrap assumes independent trades.  Overlapping
    strategies and clustered regimes violate that assumption, so the interval
    is indicative, not a guarantee.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import math
from typing import Optional

import numpy as np
import pandas as pd

from market.data_engine import epoch_seconds, normalize_epoch, timeframe_seconds
from market.features import build_features
from strategies.base import BaseStrategy
from strategies.registry import STRATEGY_CLASSES, build_strategies
from strategies.replay import ReplayConfig, ReplayResult, StrategyTrade, run_replay


# --------------------------------------------------------------------- splits
@dataclass(frozen=True)
class Fold:
    """One expanding-window fold; ``train_end == test_start``."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def train_bars(self) -> int:
        return max(0, self.train_end - self.train_start)

    @property
    def test_bars(self) -> int:
        return max(0, self.test_end - self.test_start)


@dataclass(frozen=True)
class Split:
    folds: tuple[Fold, ...]
    holdout_start: int
    holdout_end: int
    warmup_bars: int


def build_split(
    n_bars: int,
    *,
    folds: int = 4,
    holdout_fraction: float = 0.25,
    warmup_bars: int = 210,
    min_test_bars: int = 200,
) -> Split:
    """Split ``n_bars`` into contiguous expanding folds plus a holdout.

    The first test window starts after ``warmup_bars`` so every fold has
    enough history for the indicators.  The holdout occupies the tail
    ``holdout_fraction`` of the sample and is never used for fold evaluation.
    """
    n_bars = int(n_bars)
    folds = int(folds)
    warmup_bars = int(warmup_bars)
    min_test_bars = max(1, int(min_test_bars))

    if folds < 2:
        raise ValueError("walk-forward needs at least two folds")
    if not (0.0 < float(holdout_fraction) < 0.5):
        raise ValueError("holdout_fraction must be between 0 and 0.5")
    if warmup_bars < 1:
        raise ValueError("warmup_bars must be at least 1")

    holdout_start = int(round(n_bars * (1.0 - float(holdout_fraction))))
    holdout_start = min(max(holdout_start, 1), max(1, n_bars - 1))
    evaluation_bars = holdout_start - warmup_bars
    needed = folds * min_test_bars
    if evaluation_bars < needed:
        raise ValueError(
            f"not enough bars: {evaluation_bars} evaluation bars "
            f"(after {warmup_bars} warmup) for {folds} folds of at least "
            f"{min_test_bars} bars, plus a {n_bars - holdout_start}-bar holdout")

    base, remainder = divmod(evaluation_bars, folds)
    sizes = [base + (1 if index < remainder else 0) for index in range(folds)]

    windows: list[Fold] = []
    cursor = warmup_bars
    for index, size in enumerate(sizes):
        test_start = cursor
        test_end = cursor + size
        windows.append(Fold(
            index=index,
            train_start=0,
            train_end=test_start,
            test_start=test_start,
            test_end=test_end,
        ))
        cursor = test_end

    if cursor != holdout_start:
        raise AssertionError("fold boundaries do not reach the holdout")
    return Split(tuple(windows), holdout_start, n_bars, warmup_bars)


# ------------------------------------------------------------------- configs
@dataclass(frozen=True)
class SensitivityVariant:
    label: str
    overrides: dict = field(default_factory=dict)


DEFAULT_SENSITIVITY: tuple[SensitivityVariant, ...] = (
    SensitivityVariant("baseline", {}),
    SensitivityVariant("min_rr_0.6", {"min_rr": 0.6}),
    SensitivityVariant("min_rr_1.0", {"min_rr": 1.0}),
    SensitivityVariant("min_rr_1.5", {"min_rr": 1.5}),
    SensitivityVariant("min_conf_0.35", {"min_confidence": 0.35}),
    SensitivityVariant("min_conf_0.55", {"min_confidence": 0.55}),
    SensitivityVariant("max_bars_12", {"max_bars_in_trade": 12}),
    SensitivityVariant("max_bars_40", {"max_bars_in_trade": 40}),
    SensitivityVariant("atr_stop_0.8x", {"atr_stop_scale": 0.8}),
    SensitivityVariant("atr_stop_1.2x", {"atr_stop_scale": 1.2}),
)

_VARIANT_KEYS = {
    "min_rr", "min_confidence", "max_bars_in_trade",
    "atr_stop_scale", "rr_target_scale",
}


def apply_variant(config: dict | None, overrides: dict | None) -> dict:
    """Return a deep copy of ``config`` with a sensitivity variant applied."""
    out = deepcopy(config or {})
    overrides = dict(overrides or {})
    unknown = set(overrides) - _VARIANT_KEYS
    if unknown:
        raise ValueError(f"unknown sensitivity overrides: {sorted(unknown)}")

    strategies = out.setdefault("strategies", {})
    engines = strategies.setdefault("engines", {})

    if "min_rr" in overrides:
        value = float(overrides["min_rr"])
        strategies["min_rr"] = value
        for name in STRATEGY_CLASSES:
            engines.setdefault(name, {})["min_rr"] = value
    if "min_confidence" in overrides:
        strategies["min_confidence"] = float(overrides["min_confidence"])
    if "max_bars_in_trade" in overrides:
        value = int(overrides["max_bars_in_trade"])
        for name in STRATEGY_CLASSES:
            engines.setdefault(name, {})["max_bars_in_trade"] = value
    if "atr_stop_scale" in overrides:
        scale = float(overrides["atr_stop_scale"])
        for name, cls in STRATEGY_CLASSES.items():
            params = engines.setdefault(name, {})
            base = params.get("atr_stop", cls.defaults.get("atr_stop"))
            if base is not None:
                params["atr_stop"] = float(base) * scale
    if "rr_target_scale" in overrides:
        scale = float(overrides["rr_target_scale"])
        for name, cls in STRATEGY_CLASSES.items():
            params = engines.setdefault(name, {})
            base = params.get("rr_target", cls.defaults.get("rr_target"))
            if base is not None:
                params["rr_target"] = float(base) * scale
    return out


@dataclass
class WalkForwardConfig:
    """Split, evaluation and pass/fail policy for one validation run."""

    folds: int = 4
    holdout_fraction: float = 0.25
    warmup_bars: int = 210
    min_test_bars: int = 200
    tail_bars: int = 210
    mc_runs: int = 2000
    mc_seed: int = 42
    max_mc_drawdown_r: float = 10.0
    min_oos_trades: int = 30
    min_expectancy_r: float = 0.02
    min_profit_factor: float = 1.10
    min_positive_fold_share: float = 0.60
    min_mc_prob_positive: float = 0.90
    min_sensitivity_positive_share: float = 0.60
    min_holdout_trades: int = 10
    require_holdout_positive: bool = True
    run_sensitivity: bool = True
    sensitivity: Optional[tuple[SensitivityVariant, ...]] = None


# ------------------------------------------------------------------- results
@dataclass
class FoldResult:
    fold: Fold
    trades: list[StrategyTrade] = field(default_factory=list)
    proposals: int = 0
    selected: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "fold": asdict(self.fold),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "proposals": self.proposals,
            "selected": self.selected,
            "metrics": self.metrics,
            "trades": [trade.as_dict() for trade in self.trades],
        }


@dataclass
class MonteCarloResult:
    runs: int = 0
    seed: int = 0
    trades: int = 0
    total_r_p5: float = 0.0
    total_r_p50: float = 0.0
    total_r_p95: float = 0.0
    expectancy_r_p5: float = 0.0
    expectancy_r_p50: float = 0.0
    expectancy_r_p95: float = 0.0
    max_drawdown_p50: float = 0.0
    max_drawdown_p95: float = 0.0
    prob_positive: float = 0.0
    prob_profit_factor_above_one: float = 0.0
    prob_drawdown_above_limit: float = 0.0
    drawdown_limit_r: float = 0.0


@dataclass(frozen=True)
class SensitivityRow:
    label: str
    trades: int = 0
    total_r: float = 0.0
    expectancy_r: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    max_drawdown_r: float = 0.0
    overrides: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.passed or self.skipped


@dataclass
class WalkForwardResult:
    config: WalkForwardConfig
    split: Split
    folds: list[FoldResult]
    oos: ReplayResult
    holdout: ReplayResult
    monte_carlo: MonteCarloResult
    sensitivity: list[SensitivityRow]
    gates: list[GateResult]
    data_note: str = ""
    data_start_time: float = 0.0
    data_end_time: float = 0.0
    holdout_start_time: float = 0.0
    holdout_end_time: float = 0.0

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(gate.ok for gate in self.gates)

    def as_dict(self) -> dict:
        return _json_safe({
            "passed": self.passed,
            "data_note": self.data_note,
            "config": asdict(self.config),
            "split": {
                "holdout_start": self.split.holdout_start,
                "holdout_end": self.split.holdout_end,
                "warmup_bars": self.split.warmup_bars,
                "folds": [asdict(fold) for fold in self.split.folds],
            },
            "holdout_window": {
                "start_time": self.holdout_start_time,
                "end_time": self.holdout_end_time,
            },
            "sample": {
                "bars": self.split.holdout_end,
                "start_time": self.data_start_time,
                "end_time": self.data_end_time,
                "holdout_start_time": self.holdout_start_time,
                "holdout_end_time_exclusive": self.holdout_end_time,
            },
            "folds": [fold.as_dict() for fold in self.folds],
            "oos": _result_dict(self.oos),
            "holdout": _result_dict(self.holdout),
            "monte_carlo": asdict(self.monte_carlo),
            "sensitivity": [asdict(row) for row in self.sensitivity],
            "gates": [asdict(gate) for gate in self.gates],
        })


def _result_dict(result: ReplayResult) -> dict:
    return {
        "metrics": result.metrics(),
        "by_strategy": result.by_strategy(),
        "by_session": result.by_session(),
        "by_regime": result.by_regime(),
        "by_month": result.by_month(),
        "trades": [trade.as_dict() for trade in result.trades],
    }


# -------------------------------------------------------------- Monte Carlo
def bootstrap_monte_carlo(
    values: Sequence[float],
    *,
    runs: int = 2000,
    seed: int = 42,
    dd_limit_r: float = 10.0,
) -> MonteCarloResult:
    """Bootstrap trade R-multiples to estimate sampling uncertainty.

    Trades are resampled independently with replacement.  Real trades are
    correlated through regimes and overlapping strategies, so the resulting
    interval is a sanity check rather than a confidence guarantee.
    """
    returns = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))],
        dtype=float,
    )
    runs = int(runs)
    if returns.size == 0 or runs <= 0:
        return MonteCarloResult(seed=int(seed), drawdown_limit_r=float(dd_limit_r))

    rng = np.random.default_rng(int(seed))
    indices = rng.integers(0, returns.size, size=(runs, returns.size))
    samples = returns[indices]

    totals = samples.sum(axis=1)
    expectancy = samples.mean(axis=1)
    gains = np.where(samples > 0.0, samples, 0.0).sum(axis=1)
    losses = np.where(samples < 0.0, -samples, 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        profit_factor = np.where(
            losses > 0.0,
            gains / losses,
            np.where(gains > 0.0, np.inf, 0.0),
        )

    cumulative = np.cumsum(samples, axis=1)
    peaks = np.maximum.accumulate(
        np.concatenate([np.zeros((runs, 1)), cumulative], axis=1), axis=1,
    )[:, 1:]
    drawdown = (peaks - cumulative).max(axis=1)

    return MonteCarloResult(
        runs=runs,
        seed=int(seed),
        trades=int(returns.size),
        total_r_p5=float(np.percentile(totals, 5)),
        total_r_p50=float(np.percentile(totals, 50)),
        total_r_p95=float(np.percentile(totals, 95)),
        expectancy_r_p5=float(np.percentile(expectancy, 5)),
        expectancy_r_p50=float(np.percentile(expectancy, 50)),
        expectancy_r_p95=float(np.percentile(expectancy, 95)),
        max_drawdown_p50=float(np.percentile(drawdown, 50)),
        max_drawdown_p95=float(np.percentile(drawdown, 95)),
        prob_positive=float(np.mean(totals > 0.0)),
        prob_profit_factor_above_one=float(np.mean(profit_factor > 1.0)),
        prob_drawdown_above_limit=float(np.mean(drawdown > float(dd_limit_r))),
        drawdown_limit_r=float(dd_limit_r),
    )


# ------------------------------------------------------------------- gates
def evaluate_gates(
    *,
    oos_metrics: dict,
    fold_totals: Sequence[float],
    mc: MonteCarloResult,
    sensitivity: Sequence[SensitivityRow],
    holdout_metrics: dict,
    config: WalkForwardConfig,
) -> list[GateResult]:
    """Apply the documented pass/fail gates to one validation result."""
    gates: list[GateResult] = []

    trades = int(oos_metrics.get("trades", 0) or 0)
    gates.append(GateResult(
        "oos_trades",
        trades >= int(config.min_oos_trades),
        f"{trades} >= {int(config.min_oos_trades)} out-of-sample trades",
    ))

    expectancy = float(oos_metrics.get("expectancy_r", 0.0) or 0.0)
    gates.append(GateResult(
        "oos_expectancy",
        expectancy >= float(config.min_expectancy_r),
        f"{expectancy:.3f}R >= {float(config.min_expectancy_r):.3f}R per trade",
    ))

    profit_factor = float(oos_metrics.get("profit_factor", 0.0) or 0.0)
    gates.append(GateResult(
        "oos_profit_factor",
        profit_factor >= float(config.min_profit_factor),
        f"{_pf(profit_factor)} >= {float(config.min_profit_factor):.2f}",
    ))

    totals = [float(value) for value in fold_totals]
    positive_folds = sum(1 for value in totals if value > 0.0)
    fold_share = positive_folds / len(totals) if totals else 0.0
    gates.append(GateResult(
        "fold_consistency",
        fold_share >= float(config.min_positive_fold_share),
        f"{positive_folds}/{len(totals)} folds positive "
        f"({fold_share:.0%}) >= {float(config.min_positive_fold_share):.0%}",
    ))

    if mc.runs > 0:
        gates.append(GateResult(
            "mc_positive_probability",
            mc.prob_positive >= float(config.min_mc_prob_positive),
            f"P(total R > 0) = {mc.prob_positive:.1%} "
            f">= {float(config.min_mc_prob_positive):.0%}",
        ))
        gates.append(GateResult(
            "mc_drawdown",
            mc.max_drawdown_p95 <= float(config.max_mc_drawdown_r),
            f"p95 max drawdown {mc.max_drawdown_p95:.2f}R "
            f"<= {float(config.max_mc_drawdown_r):.2f}R",
        ))
    else:
        gates.append(GateResult(
            "mc_positive_probability", False,
            "no out-of-sample trades to resample",
        ))
        gates.append(GateResult(
            "mc_drawdown", False,
            "no out-of-sample trades to resample",
        ))

    rows = list(sensitivity)
    if rows:
        positive = sum(1 for row in rows if float(row.total_r) > 0.0)
        share = positive / len(rows)
        gates.append(GateResult(
            "sensitivity_positive_share",
            share >= float(config.min_sensitivity_positive_share),
            f"{positive}/{len(rows)} variants positive "
            f"({share:.0%}) >= {float(config.min_sensitivity_positive_share):.0%}",
        ))
    elif not config.run_sensitivity:
        gates.append(GateResult(
            "sensitivity_positive_share", False,
            "skipped by configuration (enable sensitivity for a full verdict)",
            skipped=True,
        ))
    else:
        gates.append(GateResult(
            "sensitivity_positive_share", False,
            "no sensitivity variants were evaluated",
        ))

    holdout_trades = int(holdout_metrics.get("trades", 0) or 0)
    gates.append(GateResult(
        "holdout_trades",
        holdout_trades >= int(config.min_holdout_trades),
        f"{holdout_trades} >= {int(config.min_holdout_trades)} holdout trades",
    ))
    if config.require_holdout_positive:
        holdout_expectancy = float(holdout_metrics.get("expectancy_r", 0.0) or 0.0)
        gates.append(GateResult(
            "holdout_expectancy",
            holdout_expectancy > 0.0,
            f"{holdout_expectancy:.3f}R > 0R per holdout trade",
        ))
    else:
        gates.append(GateResult(
            "holdout_expectancy", False,
            "skipped by configuration (holdout positivity not required)",
            skipped=True,
        ))
    return gates


# ------------------------------------------------------------- orchestration
def run_walk_forward(
    frame: pd.DataFrame,
    config: dict | None = None,
    *,
    replay: ReplayConfig | None = None,
    wf: WalkForwardConfig | None = None,
    engines_factory: Callable[[dict | None], dict[str, BaseStrategy]] = build_strategies,
    data_note: str = "",
) -> WalkForwardResult:
    """Evaluate the fixed rules across expanding folds plus one holdout.

    ``engines_factory`` exists so tests can inject deterministic engines; the
    production path always uses :func:`strategies.registry.build_strategies`.
    Every fold and every sensitivity variant receives a fresh engine set, so
    no state leaks between windows.
    """
    wf = wf or WalkForwardConfig()
    prepared = _prepare_frame(frame)
    replay_cfg = replace(
        replay or ReplayConfig.from_config(config),
        warmup_bars=int(wf.warmup_bars),
    )
    split = build_split(
        len(prepared),
        folds=wf.folds,
        holdout_fraction=wf.holdout_fraction,
        warmup_bars=wf.warmup_bars,
        min_test_bars=wf.min_test_bars,
    )
    features = build_features(
        prepared, (config or {}).get("market", {}).get("features"))

    def run_window(start: int, end: int, run_config: dict | None,
                   tail: int) -> tuple[ReplayResult, list[StrategyTrade],
                                        float, float]:
        lo = max(0, start - int(wf.warmup_bars))
        hi = min(len(features), end + max(0, int(tail)))
        if end < split.holdout_start and tail > 0:
            hi = min(hi, split.holdout_start)
        engines = engines_factory(run_config)
        result = run_replay(
            features.iloc[lo:hi], run_config, replay=replay_cfg,
            strategies=engines, features_ready=True, data_note=data_note,
        )
        start_time = _time_at(features, start, replay_cfg)
        end_time = _time_at(features, end, replay_cfg)
        trades = [
            trade for trade in result.trades
            if start_time <= trade.entry_time < end_time
        ]
        return result, trades, start_time, end_time

    fold_results: list[FoldResult] = []
    strategy_names: list[str] = []
    for fold in split.folds:
        raw, trades, start_time, end_time = run_window(
            fold.test_start, fold.test_end, config, wf.tail_bars)
        if not strategy_names:
            strategy_names = list(raw.strategies)
        fold_results.append(FoldResult(
            fold=fold,
            trades=trades,
            proposals=raw.proposals,
            selected=raw.selected,
            start_time=start_time,
            end_time=end_time,
            metrics=ReplayResult(trades=trades).metrics(),
        ))

    _, holdout_trades, holdout_start_time, holdout_end_time = run_window(
        split.holdout_start, split.holdout_end, config, 0)
    holdout = ReplayResult(
        trades=holdout_trades,
        strategies=list(strategy_names),
        config=replay_cfg,
        data_note=f"{data_note} | untouched holdout",
    )

    oos_trades = [trade for fold in fold_results for trade in fold.trades]
    oos = ReplayResult(
        trades=oos_trades,
        strategies=list(strategy_names),
        config=replay_cfg,
        data_note=f"{data_note} | out-of-sample folds",
    )

    monte_carlo = bootstrap_monte_carlo(
        [trade.r_net for trade in oos_trades],
        runs=wf.mc_runs,
        seed=wf.mc_seed,
        dd_limit_r=wf.max_mc_drawdown_r,
    )

    sensitivity: list[SensitivityRow] = []
    if wf.run_sensitivity:
        variants = wf.sensitivity if wf.sensitivity is not None else DEFAULT_SENSITIVITY
        for variant in variants:
            variant_config = apply_variant(config, variant.overrides)
            variant_trades: list[StrategyTrade] = []
            for fold in split.folds:
                _, trades, _, _ = run_window(
                    fold.test_start, fold.test_end, variant_config, wf.tail_bars)
                variant_trades.extend(trades)
            metrics = ReplayResult(trades=variant_trades).metrics()
            sensitivity.append(SensitivityRow(
                label=variant.label,
                trades=int(metrics["trades"]),
                total_r=float(metrics["total_r"]),
                expectancy_r=float(metrics["expectancy_r"]),
                profit_factor=float(metrics["profit_factor"]),
                win_rate=float(metrics["win_rate"]),
                max_drawdown_r=float(metrics["max_drawdown_r"]),
                overrides=dict(variant.overrides),
            ))

    gates = evaluate_gates(
        oos_metrics=oos.metrics(),
        fold_totals=[fold.metrics.get("total_r", 0.0) for fold in fold_results],
        mc=monte_carlo,
        sensitivity=sensitivity,
        holdout_metrics=holdout.metrics(),
        config=wf,
    )

    return WalkForwardResult(
        config=wf,
        split=split,
        folds=fold_results,
        oos=oos,
        holdout=holdout,
        monte_carlo=monte_carlo,
        sensitivity=sensitivity,
        gates=gates,
        data_note=data_note,
        data_start_time=float(features.iloc[0]["time"]),
        data_end_time=float(features.iloc[-1]["time"]),
        holdout_start_time=holdout_start_time,
        holdout_end_time=holdout_end_time,
    )


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        raise ValueError("walk-forward needs a non-empty OHLCV frame")
    missing = {"time", "open", "high", "low", "close"} - set(frame.columns)
    if missing:
        raise ValueError(f"walk-forward frame is missing columns: {sorted(missing)}")
    out = frame.copy()
    if not pd.api.types.is_numeric_dtype(out["time"]):
        out["time"] = epoch_seconds(out["time"])
    else:
        out["time"] = normalize_epoch(out["time"])
    if not out["time"].is_monotonic_increasing:
        out = out.sort_values("time")
    return out.reset_index(drop=True)


def _time_at(frame: pd.DataFrame, index: int, cfg: ReplayConfig) -> float:
    if index < len(frame):
        return float(frame.iloc[index]["time"])
    return float(frame.iloc[-1]["time"]) + timeframe_seconds(cfg.timeframe)


# ------------------------------------------------------------------ report
def format_report(result: WalkForwardResult) -> str:
    wf = result.config
    cfg = result.oos.config or ReplayConfig()
    split = result.split
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("WALK-FORWARD / HOLDOUT VALIDATION (fixed rules, net-of-cost R)")
    lines.append("=" * 80)
    lines.append(f"data                  : {result.data_note or 'dataframe'}")
    lines.append(f"symbol/timeframe      : {cfg.symbol} {cfg.timeframe} "
                 f"({split.holdout_end} bars)")
    lines.append(f"sample                : {_fmt_ts(result.data_start_time)} -> "
                 f"{_fmt_ts(result.data_end_time)} UTC (last closed bar)")
    lines.append(f"trades                : {len(result.oos.trades)} OOS + "
                 f"{len(result.holdout.trades)} holdout attributed by entry time")
    lines.append(f"split                 : {len(split.folds)} expanding folds + "
                 f"untouched holdout ({wf.holdout_fraction:.0%} of bars)")
    lines.append(f"holdout               : {_fmt_ts(result.holdout_start_time)} -> "
                 f"{_fmt_ts(result.holdout_end_time)} UTC (exclusive end)")
    lines.append(f"warmup / tail         : {wf.warmup_bars} bars / {wf.tail_bars} bars")
    lines.append(f"costs                 : spread {cfg.spread_points:.1f} pts + "
                 f"slippage {cfg.slippage_points:.1f} pts per side")
    lines.append("attribution           : a trade belongs to the fold where it was entered")
    lines.append("notes                 : fixed rules, no parameter fitting; folds test "
                 "temporal stability")
    lines.append("")

    lines.append("Folds (out-of-sample test windows)")
    header = (f"{'fold':>4s} {'test window (UTC)':28s} {'bars':>5s} {'trades':>6s} "
              f"{'win%':>6s} {'totalR':>8s} {'expR':>7s} {'PF':>6s} {'maxDD':>7s}")
    lines.append(header)
    lines.append("-" * len(header))
    for fold_result in result.folds:
        metrics = fold_result.metrics
        window = (f"{_fmt_ts(fold_result.start_time, short=True)} -> "
                  f"{_fmt_ts(fold_result.end_time, short=True)}")
        lines.append(
            f"{fold_result.fold.index + 1:4d} {window:28s} "
            f"{fold_result.fold.test_bars:5d} {metrics['trades']:6d} "
            f"{metrics['win_rate']:6.1f} {metrics['total_r']:8.2f} "
            f"{metrics['expectancy_r']:7.3f} {_pf(metrics['profit_factor']):>6s} "
            f"{metrics['max_drawdown_r']:7.2f}")
    lines.append("")

    lines.append("OOS aggregate (all folds combined)")
    lines.append(_strategy_table(result.oos))
    lines.append("")

    lines.append("Holdout (untouched, evaluated once, never used for selection)")
    lines.append(_strategy_table(result.holdout))
    lines.append("")

    mc = result.monte_carlo
    lines.append(f"Monte Carlo bootstrap ({mc.runs} resamples, seed {mc.seed}, "
                 f"{mc.trades} trades; iid caveat)")
    if mc.runs:
        lines.append(f"total R       p5={mc.total_r_p5:8.2f}  "
                     f"p50={mc.total_r_p50:8.2f}  p95={mc.total_r_p95:8.2f}")
        lines.append(f"expectancy    p5={mc.expectancy_r_p5:8.3f}  "
                     f"p50={mc.expectancy_r_p50:8.3f}  "
                     f"p95={mc.expectancy_r_p95:8.3f} R")
        lines.append(f"max drawdown  p50={mc.max_drawdown_p50:7.2f}  "
                     f"p95={mc.max_drawdown_p95:7.2f} R")
        lines.append(f"P(total R > 0) = {mc.prob_positive:.1%}   "
                     f"P(PF > 1) = {mc.prob_profit_factor_above_one:.1%}   "
                     f"P(maxDD > {mc.drawdown_limit_r:.1f}R) = "
                     f"{mc.prob_drawdown_above_limit:.1%}")
    else:
        lines.append("no out-of-sample trades; nothing to resample")
    lines.append("")

    lines.append("Parameter sensitivity (OOS folds only; holdout excluded)")
    if result.sensitivity:
        header = (f"{'variant':20s} {'trades':>6s} {'win%':>6s} {'totalR':>8s} "
                  f"{'expR':>7s} {'PF':>6s} {'maxDD':>7s}")
        lines.append(header)
        lines.append("-" * len(header))
        for row in result.sensitivity:
            lines.append(
                f"{row.label:20s} {row.trades:6d} {row.win_rate:6.1f} "
                f"{row.total_r:8.2f} {row.expectancy_r:7.3f} "
                f"{_pf(row.profit_factor):>6s} {row.max_drawdown_r:7.2f}")
        positive = sum(1 for row in result.sensitivity if row.total_r > 0.0)
        lines.append(f"positive variants     : {positive}/{len(result.sensitivity)} "
                     f"({positive / len(result.sensitivity):.0%})")
    else:
        lines.append("skipped by configuration")
    lines.append("")

    lines.append("Gates")
    for gate in result.gates:
        mark = "SKIP" if gate.skipped else ("PASS" if gate.passed else "FAIL")
        lines.append(f"[{mark}] {gate.name:28s} {gate.detail}")
    passed = sum(1 for gate in result.gates if gate.passed)
    skipped = sum(1 for gate in result.gates if gate.skipped)
    failed = len(result.gates) - passed - skipped
    lines.append("")
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"VERDICT: {verdict} ({passed}/{len(result.gates)} passed, "
                 f"{failed} failed, {skipped} skipped)")
    lines.append("A PASS is necessary, not sufficient: it does not authorise live "
                 "trading or override the risk governor.")
    lines.append("No parameter was selected on the holdout.  Negative or "
                 "inconclusive results must not be promoted.")
    if "demo_1min" in (result.data_note or ""):
        lines.append("Synthetic fixture data is a determinism check, not an edge claim.")
    lines.append("=" * 80)
    return "\n".join(lines)


def _strategy_table(result: ReplayResult) -> str:
    header = (f"{'strategy':16s} {'trades':>6s} {'win%':>6s} {'totalR':>8s} "
              f"{'expR':>7s} {'PF':>6s} {'maxDD':>7s} {'sharpe':>7s}")
    lines = [header, "-" * len(header)]
    for name in result.strategies:
        lines.append(_metric_line(name, result.metrics(name)))
    lines.append("-" * len(header))
    lines.append(_metric_line("COMBINED", result.metrics()))
    return "\n".join(lines)


def _metric_line(name: str, metrics: dict) -> str:
    return (f"{name:16s} {metrics['trades']:6d} {metrics['win_rate']:6.1f} "
            f"{metrics['total_r']:8.2f} {metrics['expectancy_r']:7.3f} "
            f"{_pf(metrics['profit_factor']):>6s} "
            f"{metrics['max_drawdown_r']:7.2f} {metrics['sharpe']:7.2f}")


def _pf(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{float(value):.2f}"


def _fmt_ts(value: float, *, short: bool = False) -> str:
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "?"
    return moment.strftime("%Y-%m-%d %H:%M" if not short else "%m-%d %H:%M")


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


__all__ = [
    "DEFAULT_SENSITIVITY",
    "Fold",
    "FoldResult",
    "GateResult",
    "MonteCarloResult",
    "SensitivityRow",
    "SensitivityVariant",
    "Split",
    "WalkForwardConfig",
    "WalkForwardResult",
    "apply_variant",
    "bootstrap_monte_carlo",
    "build_split",
    "evaluate_gates",
    "format_report",
    "run_walk_forward",
]
