"""Walk-forward / holdout validation: splits, isolation, Monte Carlo, gates."""
from __future__ import annotations

import pandas as pd
import pytest

from research.walkforward import (
    GateResult,
    MonteCarloResult,
    SensitivityRow,
    WalkForwardConfig,
    apply_variant,
    bootstrap_monte_carlo,
    build_split,
    evaluate_gates,
    format_report,
    run_walk_forward,
)
from strategies.base import BaseStrategy
from strategies.replay import ReplayConfig
from strategies.trend import TrendStrategy


class EmitFirstStrategy(BaseStrategy):
    """Emit one signal on the first bar of each replay, then hold."""

    name = "trend"

    def __init__(self, params: dict | None = None):
        super().__init__({
            "enabled": True,
            "min_bars": 1,
            "min_rr": 0.1,
            "max_spread_atr": 0.0,
            "sessions": [],
            "max_bars_in_trade": 100,
        })
        self.seen: list[float] = []
        self.emitted = False

    def evaluate(self, ctx):
        self.seen.append(ctx.bar_time)
        if self.emitted:
            return []
        self.emitted = True
        signal = ctx.make_signal(
            self.name, 1, 100.0, 99.0, 103.0, 0.90,
            min_rr=0.1, expires_bars=10,
        )
        return [signal] if signal else []


def _frame(n: int = 64, step: int = 900) -> pd.DataFrame:
    return pd.DataFrame([{
        "time": 1_700_000_000 + i * step,
        "open": 100.0,
        "high": 100.2,
        "low": 99.8,
        "close": 100.0,
        "tick_volume": 100.0,
    } for i in range(n)])


def _wf(**overrides) -> WalkForwardConfig:
    values = dict(
        folds=4,
        holdout_fraction=0.25,
        warmup_bars=1,
        min_test_bars=4,
        tail_bars=2,
        mc_runs=200,
        mc_seed=7,
        run_sensitivity=False,
        min_oos_trades=1,
        min_holdout_trades=1,
    )
    values.update(overrides)
    return WalkForwardConfig(**values)


def _replay() -> ReplayConfig:
    return ReplayConfig(
        warmup_bars=1,
        spread_points=0.0,
        slippage_points=0.0,
        max_open_positions=1,
    )


def _run(frame: pd.DataFrame, wf: WalkForwardConfig | None = None):
    probes: list[EmitFirstStrategy] = []

    def factory(_config):
        probe = EmitFirstStrategy()
        probes.append(probe)
        return {"trend": probe}

    result = run_walk_forward(
        frame, None, replay=_replay(), wf=wf or _wf(),
        engines_factory=factory, data_note="test fixture",
    )
    return result, probes


# --------------------------------------------------------------------- splits
def test_build_split_is_contiguous_and_reserves_an_untouched_holdout():
    split = build_split(1000, folds=4, holdout_fraction=0.2,
                        warmup_bars=100, min_test_bars=50)
    assert split.holdout_start == 800
    assert split.holdout_end == 1000
    assert [fold.test_start for fold in split.folds] == [100, 275, 450, 625]
    assert [fold.test_end for fold in split.folds] == [275, 450, 625, 800]
    assert all(fold.train_start == 0 for fold in split.folds)
    assert all(fold.train_end == fold.test_start for fold in split.folds)
    assert split.folds[-1].test_end == split.holdout_start


def test_build_split_rejects_a_short_sample():
    with pytest.raises(ValueError, match="not enough bars"):
        build_split(200, folds=4, holdout_fraction=0.25,
                    warmup_bars=100, min_test_bars=30)


def test_build_split_requires_at_least_two_folds():
    with pytest.raises(ValueError, match="at least two folds"):
        build_split(1000, folds=1, holdout_fraction=0.25,
                    warmup_bars=100, min_test_bars=50)


# ---------------------------------------------------------- fold attribution
def test_walk_forward_attributes_trades_by_entry_time_and_never_touches_holdout():
    result, probes = _run(_frame(64))
    split = result.split

    assert len(result.folds) == 4
    assert len(probes) == 5  # four folds + one untouched holdout run
    for fold_result, probe in zip(result.folds, probes[:4]):
        fold = fold_result.fold
        start_time = 1_700_000_000 + fold.test_start * 900
        end_time = 1_700_000_000 + fold.test_end * 900
        assert probe.seen[0] == start_time
        assert min(probe.seen) >= start_time
        assert max(probe.seen) < 1_700_000_000 + split.holdout_start * 900
        assert len(fold_result.trades) == 1
        assert start_time <= fold_result.trades[0].entry_time < end_time

    holdout_start_time = 1_700_000_000 + split.holdout_start * 900
    assert holdout_start_time == 1_700_000_000 + split.holdout_start * 900
    assert probes[-1].seen[0] == holdout_start_time
    assert min(probes[-1].seen) >= holdout_start_time
    assert len(result.holdout.trades) == 1
    assert result.holdout.trades[0].entry_time >= holdout_start_time
    assert all(trade.entry_time < holdout_start_time
               for trade in result.oos.trades)
    assert result.data_end_time == 1_700_000_000 + 63 * 900


# ------------------------------------------------------------- Monte Carlo
def test_monte_carlo_is_deterministic_for_a_seed():
    values = [1.0, -1.0, 2.0, 0.5, -1.0, 0.25]
    first = bootstrap_monte_carlo(values, runs=500, seed=11)
    second = bootstrap_monte_carlo(values, runs=500, seed=11)
    assert first == second
    assert 0.0 <= first.prob_positive <= 1.0


def test_monte_carlo_all_winners_always_positive():
    result = bootstrap_monte_carlo([1.0, 2.0, 0.5], runs=200, seed=3)
    assert result.prob_positive == 1.0
    assert result.prob_profit_factor_above_one == 1.0
    assert result.total_r_p5 > 0.0


def test_monte_carlo_all_losers_never_positive():
    result = bootstrap_monte_carlo([-1.0, -2.0, -0.5], runs=200, seed=3)
    assert result.prob_positive == 0.0
    assert result.prob_profit_factor_above_one == 0.0
    assert result.total_r_p95 <= 0.0


def test_monte_carlo_handles_an_empty_trade_list():
    result = bootstrap_monte_carlo([], runs=100, seed=1)
    assert result.runs == 0
    assert result.prob_positive == 0.0
    assert result.total_r_p50 == 0.0


# ------------------------------------------------------------ parameter grid
def test_apply_variant_updates_arbiter_and_engine_thresholds_without_mutating():
    base = {"strategies": {"min_rr": 0.8, "min_confidence": 0.45, "engines": {}}}
    variant = apply_variant(base, {"min_rr": 1.2, "min_confidence": 0.55})
    assert variant["strategies"]["min_rr"] == 1.2
    assert variant["strategies"]["min_confidence"] == 0.55
    for name in ("trend", "breakout", "smc", "momentum", "mean_reversion"):
        assert variant["strategies"]["engines"][name]["min_rr"] == 1.2
    assert base["strategies"]["min_rr"] == 0.8


def test_apply_variant_scales_atr_stops_from_engine_defaults():
    base = {"strategies": {"engines": {}}}
    variant = apply_variant(base, {"atr_stop_scale": 2.0})
    expected = TrendStrategy.defaults["atr_stop"] * 2.0
    assert variant["strategies"]["engines"]["trend"]["atr_stop"] == pytest.approx(expected)


# ------------------------------------------------------------------- gates
def _strong_mc() -> MonteCarloResult:
    return MonteCarloResult(
        runs=500, seed=1, trades=60,
        total_r_p5=1.0, total_r_p50=8.0, total_r_p95=15.0,
        expectancy_r_p5=0.02, expectancy_r_p50=0.13, expectancy_r_p95=0.25,
        max_drawdown_p50=2.0, max_drawdown_p95=4.0,
        prob_positive=0.97, prob_profit_factor_above_one=0.95,
        prob_drawdown_above_limit=0.0, drawdown_limit_r=10.0,
    )


def _positive_sensitivity() -> list[SensitivityRow]:
    return [
        SensitivityRow(label="baseline", total_r=8.0, expectancy_r=0.13),
        SensitivityRow(label="min_rr_1.0", total_r=3.0, expectancy_r=0.05),
        SensitivityRow(label="min_rr_1.5", total_r=1.0, expectancy_r=0.02),
    ]


def test_gates_pass_on_a_strong_out_of_sample_result():
    gates = evaluate_gates(
        oos_metrics={"trades": 60, "expectancy_r": 0.13, "profit_factor": 1.5},
        fold_totals=[1.0, 2.0, 3.0, 2.0],
        mc=_strong_mc(),
        sensitivity=_positive_sensitivity(),
        holdout_metrics={"trades": 15, "expectancy_r": 0.10, "profit_factor": 1.2},
        config=WalkForwardConfig(),
    )
    by_name = {gate.name: gate for gate in gates}
    assert all(gate.passed for gate in gates), by_name
    assert by_name["oos_trades"].passed
    assert by_name["holdout_expectancy"].passed


def test_gates_fail_on_a_weak_or_empty_result():
    winners = [SensitivityRow(label=f"v{i}", total_r=-2.0) for i in range(3)]
    gates = evaluate_gates(
        oos_metrics={"trades": 2, "expectancy_r": -0.5, "profit_factor": 0.4},
        fold_totals=[-2.0, -1.0, -2.0, -3.0],
        mc=MonteCarloResult(runs=0),
        sensitivity=winners,
        holdout_metrics={"trades": 1, "expectancy_r": -0.2, "profit_factor": 0.1},
        config=WalkForwardConfig(),
    )
    by_name = {gate.name: gate for gate in gates}
    assert not by_name["oos_trades"].passed
    assert not by_name["oos_expectancy"].passed
    assert not by_name["oos_profit_factor"].passed
    assert not by_name["fold_consistency"].passed
    assert not by_name["mc_positive_probability"].passed
    assert not by_name["mc_drawdown"].passed
    assert not by_name["sensitivity_positive_share"].passed
    assert not by_name["holdout_trades"].passed
    assert not by_name["holdout_expectancy"].passed
    assert isinstance(by_name["oos_trades"], GateResult)


def test_disabled_sensitivity_gate_is_skipped_not_silently_passed():
    gates = evaluate_gates(
        oos_metrics={"trades": 60, "expectancy_r": 0.13, "profit_factor": 1.5},
        fold_totals=[1.0, 2.0, 3.0, 2.0],
        mc=_strong_mc(),
        sensitivity=[],
        holdout_metrics={"trades": 15, "expectancy_r": 0.10, "profit_factor": 1.2},
        config=WalkForwardConfig(run_sensitivity=False),
    )
    gate = {item.name: item for item in gates}["sensitivity_positive_share"]
    assert gate.skipped is True
    assert gate.ok is True
    assert "skipped" in gate.detail.lower()


# ------------------------------------------------------------------ report
def test_report_contains_every_section_and_an_honest_verdict():
    result, _ = _run(_frame(64), _wf(folds=3, run_sensitivity=True))
    report = format_report(result)
    for token in (
        "WALK-FORWARD", "Fold", "OOS", "Holdout", "Monte Carlo",
        "Parameter sensitivity", "Gates", "VERDICT",
        "necessary, not sufficient",
    ):
        assert token in report
    assert "test fixture" in report
    assert result.passed is False  # flat fixture has no edge


# ---------------------------------------------------------------------- CLI
def test_cli_walkforward_writes_json_and_returns_a_gate_code(tmp_path, capsys):
    import json
    from argparse import Namespace

    from main import cmd_walkforward

    csv_path = tmp_path / "bars.csv"
    _frame(900).to_csv(csv_path, index=False)
    json_path = tmp_path / "reports" / "walkforward.json"
    args = Namespace(
        csv=str(csv_path), symbol=None, bars=5000, only=None,
        point=None, digits=None, spread_points=None, slippage_points=None,
        warmup=1, folds=3, holdout=0.25, min_test_bars=200,
        mc_runs=100, seed=5, json=str(json_path), no_sensitivity=True,
    )
    cfg = {"trading": {"symbol": "XAUUSD", "timeframe": "M15"}}

    code = cmd_walkforward(cfg, args)

    assert code in (0, 1)
    assert "WALK-FORWARD" in capsys.readouterr().out
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["split"]["holdout_start"] > 0
    assert payload["sample"]["bars"] == 900
    assert payload["sample"]["end_time"] == 1_700_000_000 + 899 * 900
    assert payload["passed"] is False
    assert payload["gates"]
