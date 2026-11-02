"""The backtest CLI must resolve to backtest.py, not a package."""
from __future__ import annotations

import json
import os

import backtest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_backtest_module_is_the_top_level_file():
    assert os.path.basename(backtest.__file__) == "backtest.py"
    assert callable(backtest.run)
    assert callable(backtest.load_csv)
    assert callable(backtest.print_result)


def test_research_package_is_importable_separately():
    from research import backtest as research_bt
    assert callable(research_bt.replay)


def test_short_csv_backtest_is_deterministic():
    cfg_path = os.path.join(ROOT, "config.example.json")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    df = backtest.load_csv(os.path.join(ROOT, "demo_1min.csv"))
    sample = df.tail(1200)
    first = backtest.run(sample, cfg, warmup=200)
    second = backtest.run(sample, cfg, warmup=200)
    assert first.trades == second.trades
    assert first.total_R == second.total_R
    assert first.equity_curve_R == second.equity_curve_R
    assert first.trades >= 0
