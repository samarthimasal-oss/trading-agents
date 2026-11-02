"""Replay harness: next-bar fills, conservative exits, costs and metrics."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import BaseStrategy
from strategies.replay import ReplayConfig, format_report, run_replay


class OneShotStrategy(BaseStrategy):
    """Emit exactly one proposal, at a chosen bar, for deterministic tests."""

    name = "trend"

    def __init__(self, strategy_name: str = "trend", signal_at: int = 2,
                 direction: int = 1, stop: float = 99.0,
                 target: float = 103.0, max_bars: int = 100):
        self.name = strategy_name
        self.signal_at = signal_at
        self.direction = direction
        self.stop = stop
        self.target = target
        super().__init__({
            "enabled": True,
            "min_bars": 1,
            "min_rr": 0.1,
            "max_spread_atr": 0.0,
            "sessions": [],
            "max_bars_in_trade": max_bars,
        })

    def evaluate(self, ctx):
        if ctx.index != self.signal_at:
            return []
        signal = ctx.make_signal(
            self.name, self.direction, 100.0, self.stop, self.target,
            0.90, min_rr=0.1, expires_bars=10,
        )
        return [signal] if signal else []


def _frame(n: int = 10) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "time": 1_700_000_000 + i * 900,
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "atr": 1.0,
            "session": "london",
        })
    return pd.DataFrame(rows)


def _replay(**overrides) -> ReplayConfig:
    values = {
        "warmup_bars": 1,
        "spread_points": 0.0,
        "slippage_points": 0.0,
        "point": 0.01,
        "digits": 2,
        "max_open_positions": 2,
    }
    values.update(overrides)
    return ReplayConfig(**values)


def _run(frame, strategy, replay=None):
    return run_replay(
        frame, None, replay=replay or _replay(),
        strategies={strategy.name: strategy}, features_ready=True,
        data_note="test fixture",
    )


def test_signal_fills_at_next_bar_open_and_target_is_honoured():
    frame = _frame()
    frame.loc[4, ["high", "low", "close"]] = [103.5, 99.8, 103.0]
    result = _run(frame, OneShotStrategy())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == 1_700_000_000 + 3 * 900
    assert trade.entry == pytest.approx(100.0)
    assert trade.exit == pytest.approx(103.0)
    assert trade.exit_reason == "target"
    assert trade.r_net == pytest.approx(3.0)
    assert trade.bars_held == 1
    assert result.metrics()["trades"] == 1
    assert result.metrics()["total_r"] == pytest.approx(3.0)
    assert result.metrics()["profit_factor"] == float("inf")


def test_same_bar_stop_and_target_resolves_to_stop():
    frame = _frame()
    frame.loc[3, ["high", "low", "close"]] = [104.0, 98.0, 100.0]
    result = _run(frame, OneShotStrategy())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.r_net == pytest.approx(-1.0)
    assert trade.mae_r >= 1.0


def test_signal_bar_itself_is_never_the_fill_bar():
    frame = _frame()
    frame.loc[2, ["high", "low", "close"]] = [110.0, 90.0, 100.0]
    frame.loc[3, ["high", "low", "close"]] = [100.2, 99.9, 100.0]
    frame.loc[4, ["high", "low", "close"]] = [103.5, 99.8, 103.0]
    result = _run(frame, OneShotStrategy())
    assert result.trades[0].entry_time == 1_700_000_000 + 3 * 900
    assert result.trades[0].exit_time == 1_700_000_000 + 4 * 900


def test_costs_reduce_r_and_are_reported():
    frame = _frame()
    frame.loc[4, ["high", "low", "close"]] = [103.5, 99.8, 103.0]
    replay = _replay(spread_points=10.0, slippage_points=1.0)
    result = _run(frame, OneShotStrategy(), replay=replay)
    trade = result.trades[0]
    assert trade.r_net < trade.r_gross
    assert trade.cost_r > 0
    assert trade.entry > 100.0
    assert trade.exit < 103.0


def test_time_exit_fills_at_the_next_open():
    frame = _frame()
    frame.loc[3, ["high", "low", "close"]] = [100.4, 99.7, 100.0]
    frame.loc[4, ["high", "low", "close"]] = [100.5, 99.6, 100.0]
    frame.loc[5, "open"] = 100.2
    result = _run(frame, OneShotStrategy(max_bars=1))
    trade = result.trades[0]
    assert trade.exit_reason == "time exit"
    assert trade.exit_time == 1_700_000_000 + 5 * 900
    assert trade.exit == pytest.approx(100.2)


def test_gap_through_stop_fills_at_the_gap_open():
    frame = _frame()
    frame.loc[4, ["open", "high", "low", "close"]] = [97.0, 97.5, 96.5, 97.0]
    result = _run(frame, OneShotStrategy())
    trade = result.trades[0]
    assert trade.entry_time == 1_700_000_000 + 3 * 900
    assert trade.exit_reason == "gap stop"
    assert trade.exit == pytest.approx(97.0)
    assert trade.r_net < -1.0


def test_max_open_positions_rejects_second_signal():
    frame = _frame()
    frame.loc[4, ["high", "low", "close"]] = [103.5, 99.8, 103.0]
    engines = {
        "trend": OneShotStrategy("trend"),
        "breakout": OneShotStrategy("breakout"),
    }
    result = run_replay(
        frame, None, replay=_replay(max_open_positions=1),
        strategies=engines, features_ready=True,
    )
    assert len(result.trades) == 1
    assert result.rejections.get("max open positions") == 1


def test_disabled_engine_is_not_evaluated():
    frame = _frame()
    strategy = OneShotStrategy()
    strategy.enabled = False
    result = _run(frame, strategy)
    assert not result.trades


def test_metrics_breakdowns_and_report_text():
    frame = _frame()
    frame.loc[4, ["high", "low", "close"]] = [103.5, 99.8, 103.0]
    result = _run(frame, OneShotStrategy())
    assert result.by_session()["london"]["trades"] == 1
    assert result.by_regime()["unknown"]["trades"] == 1
    assert result.by_month()
    report = format_report(result)
    assert "COMBINED" in report
    assert "trend" in report
    assert "not a live performance claim" in report
    assert "next-bar open" in report


def test_short_target_and_stop_directions():
    frame = _frame()
    frame.loc[4, ["high", "low", "close"]] = [100.2, 96.5, 97.0]
    strategy = OneShotStrategy(direction=-1, stop=101.0, target=97.0)
    result = _run(frame, strategy)
    trade = result.trades[0]
    assert trade.direction == -1
    assert trade.exit_reason == "target"
    assert trade.r_net == pytest.approx(3.0)
