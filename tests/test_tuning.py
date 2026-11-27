"""Tests for the cost-aware three-way tuning study."""
from __future__ import annotations

import json

import pytest

from research import tune


# --------------------------------------------------------------------- split
def test_study_split_is_chronological_and_non_overlapping():
    split = tune.build_study_split(1000, warmup_bars=100, tail_bars=10)
    assert split.train_start == 100
    assert split.train_end == split.validation_start
    assert split.validation_end == split.holdout_start
    assert split.holdout_end == 990
    assert 0 < split.train_end - split.train_start
    assert 0 < split.validation_end - split.validation_start
    assert 0 < split.holdout_end - split.holdout_start
    assert (split.train_end - split.train_start
            + split.validation_end - split.validation_start
            + split.holdout_end - split.holdout_start) == 890
    # default 50/25/25 of the 890 evaluable bars
    assert split.train_end - split.train_start == 446
    assert split.validation_end - split.validation_start == 222
    assert split.holdout_end - split.holdout_start == 222


def test_study_split_rejects_bad_fractions_and_short_samples():
    with pytest.raises(ValueError, match="sum to 1"):
        tune.build_study_split(1000, train_fraction=0.5,
                               validation_fraction=0.3, holdout_fraction=0.3)
    with pytest.raises(ValueError, match="positive"):
        tune.build_study_split(1000, train_fraction=0.0)
    with pytest.raises(ValueError, match="warmup_bars"):
        tune.build_study_split(1000, warmup_bars=-1)
    with pytest.raises(ValueError, match="not enough bars"):
        tune.build_study_split(250, warmup_bars=100, min_window_bars=100)


# ------------------------------------------------------------------ variants
def test_candidate_exit_variants_apply_to_a_copy():
    cfg = {
        "agents": {"weight_trend": 1.1, "consensus_threshold": 0.30,
                   "atr_tp_mult": 3.0},
        "risk": {"dead_market_atr_ratio": 0.62,
                 "partial_close_enabled": True,
                 "trailing_atr": 1.0, "trailing_step_atr": 0.25},
    }
    weights = (("trend", 1.4), ("momentum", 0.8), ("reversion", 0.3),
               ("breakout", 0.8), ("pullback", 0.5))

    wider = tune.Candidate("trend_hi", weights, 0.40, 0.75, "wider_tp")
    applied = wider.apply(cfg)
    assert applied["agents"]["atr_tp_mult"] == pytest.approx(4.0)
    assert applied["agents"]["weight_trend"] == pytest.approx(1.4)
    assert applied["agents"]["consensus_threshold"] == pytest.approx(0.40)
    assert applied["risk"]["dead_market_atr_ratio"] == pytest.approx(0.75)
    # the source config is never mutated
    assert cfg["agents"]["atr_tp_mult"] == pytest.approx(3.0)
    assert cfg["risk"]["dead_market_atr_ratio"] == pytest.approx(0.62)

    no_partial = tune.Candidate("x", weights, 0.30, 0.62, "no_partial")
    assert no_partial.apply(cfg)["risk"]["partial_close_enabled"] is False

    no_trail = tune.Candidate("x", weights, 0.30, 0.62, "no_trail")
    moved = no_trail.apply(cfg)["risk"]
    assert moved["trailing_atr"] == pytest.approx(0.0)
    assert moved["trailing_step_atr"] == pytest.approx(0.0)


def test_candidate_patch_only_contains_changes():
    cfg = {
        "agents": {"weight_trend": 1.1, "weight_momentum": 0.9,
                   "weight_reversion": 0.4, "weight_breakout": 1.0,
                   "weight_pullback": 0.6, "consensus_threshold": 0.30,
                   "atr_tp_mult": 3.0},
        "risk": {"dead_market_atr_ratio": 0.62,
                 "partial_close_enabled": True,
                 "trailing_atr": 1.0, "trailing_step_atr": 0.25},
    }
    live = tune.baseline_candidate(cfg)
    assert live.config_patch(cfg) == {}

    challenger = tune.Candidate(
        "trend_hi",
        (("trend", 1.4), ("momentum", 0.8), ("reversion", 0.3),
         ("breakout", 0.8), ("pullback", 0.5)),
        0.40, 0.75, "wider_tp")
    patch = challenger.config_patch(cfg)
    assert patch["agents"]["consensus_threshold"] == pytest.approx(0.40)
    assert patch["agents"]["atr_tp_mult"] == pytest.approx(4.0)
    assert patch["risk"]["dead_market_atr_ratio"] == pytest.approx(0.75)
    assert "weight_trend" in patch["agents"]


# --------------------------------------------------------------------- costs
def test_cost_overrides_and_per_symbol_floors():
    cfg = {"backtest": {"commission_per_lot_round_turn": 5.0,
                        "slippage_points": 1.0}}
    costs = tune.resolve_costs(cfg, commission=3.5, slippage_points=1.25,
                               spread_points=99.0, start_equity=50_000.0)
    assert costs["commission_per_lot_round_turn"] == pytest.approx(3.5)
    assert costs["slippage_points"] == pytest.approx(1.25)
    assert costs["start_equity"] == pytest.approx(50_000.0)
    # known symbols keep their labelled floors; the global override is fallback
    assert tune.floor_for(costs, "XAUUSD") == pytest.approx(20.0)
    assert tune.floor_for(costs, "EURUSD") == pytest.approx(8.0)
    assert tune.floor_for(costs, "AUDUSD") == pytest.approx(99.0)


def test_evaluate_window_forwards_costs_to_replay(monkeypatch):
    captured = {}

    def fake_replay(*args, **kwargs):
        captured.update(kwargs)
        return {"trades": [], "curve": [100_000.0], "start_equity": 100_000.0}

    monkeypatch.setattr(tune.bt, "replay", fake_replay)
    caches = {"XAUUSD": {"df": [0] * 20, "votes": None, "confs": None,
                         "agents": ["trend"]}}
    candidate = tune.baseline_candidate({
        "agents": {"consensus_threshold": 0.3},
        "risk": {"dead_market_atr_ratio": 0.62},
    })
    costs = tune.resolve_costs({}, commission=2.5, slippage_points=3.0,
                               spread_points=7.0)
    out = tune.evaluate_window(
        caches, {"agents": {}, "risk": {}}, candidate, (5, 20),
        symbols=["XAUUSD"], timeframe="M15", costs=costs)
    assert captured["commission_per_lot"] == pytest.approx(2.5)
    assert captured["slippage_points"] == pytest.approx(3.0)
    assert captured["spread_floor_points"] == pytest.approx(20.0)
    assert captured["start"] == 5
    assert captured["end"] == 20
    assert out["bars"] == 15


# ----------------------------------------------------------------- frequency
def test_frequency_floor_uses_bars_not_trade_count():
    assert tune.trades_per_1000_bars(120, 1000) == pytest.approx(120.0)
    assert tune.passes_frequency_floor(120, 1000, 120.0)
    assert not tune.passes_frequency_floor(119, 1000, 120.0)
    assert tune.passes_frequency_floor(60, 500, 120.0)
    assert not tune.passes_frequency_floor(10, 0, 1.0)


def test_winner_is_selected_on_validation_only():
    rows = [
        {"label": "a", "bars": 1000,
         "metrics": {"trades": 100, "pnl": 100.0, "profit_factor": 1.10,
                     "expectancy_usd": 1.0},
         "holdout": {"pnl": 999_999.0}},
        {"label": "b", "bars": 1000,
         "metrics": {"trades": 100, "pnl": 200.0, "profit_factor": 1.20,
                     "expectancy_usd": 2.0},
         "holdout": {"pnl": -999_999.0}},
        {"label": "too_rare", "bars": 1000,
         "metrics": {"trades": 5, "pnl": 999_999.0, "profit_factor": 9.0,
                     "expectancy_usd": 99.0}},
    ]
    winner = tune.select_winner(rows, min_trades_per_1000_bars=50.0)
    assert winner["label"] == "b"
    assert tune.select_winner(
        [rows[2]], min_trades_per_1000_bars=50.0) is None


# -------------------------------------------------------------------- gates
def test_holdout_gates_require_positive_and_better_than_baseline():
    winner = {"bars": 1000,
              "metrics": {"trades": 200, "pnl": 500.0,
                          "profit_factor": 1.20, "expectancy_usd": 2.5}}
    baseline = {"bars": 1000,
                "metrics": {"trades": 200, "pnl": 100.0,
                            "profit_factor": 1.05, "expectancy_usd": 0.5}}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=120.0)
    assert all(gate.passed for gate in gates)


def test_holdout_gates_read_the_aggregate_expectancy_key():
    # summarize() emits avg_pnl_per_trade, not the per-trade row's expectancy_usd
    winner = {"bars": 1000,
              "metrics": {"trades": 200, "pnl": 500.0,
                          "profit_factor": 1.20,
                          "avg_pnl_per_trade": 2.5}}
    baseline = {"bars": 1000,
                "metrics": {"trades": 200, "pnl": 100.0,
                            "profit_factor": 1.05,
                            "avg_pnl_per_trade": 0.5}}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=120.0)
    by_name = {gate.name: gate for gate in gates}
    assert by_name["holdout_expectancy"].passed


def test_expectancy_falls_back_to_pnl_over_trades():
    # older per-symbol rows only carried pnl and trades
    assert tune._expectancy({"trades": 4, "pnl": -10.0}) == -2.5
    assert tune._expectancy({"trades": 0, "pnl": 10.0}) == 0.0
    # an explicit key always wins
    assert tune._expectancy({"trades": 4, "pnl": -10.0,
                             "avg_pnl_per_trade": 1.5}) == 1.5


def test_row_from_carries_per_trade_expectancy():
    from research.measure_upgrade import row_from

    row = row_from({"trades": 4, "pnl": -10.0, "win_rate_pct": 25.0,
                    "profit_factor": 0.5, "max_drawdown": 12.0,
                    "avg_r": -0.1}, [])
    assert row["avg_pnl_per_trade"] == -2.5


@pytest.mark.parametrize("field,value,gate_name", [
    ("expectancy_usd", -1.0, "holdout_expectancy"),
    ("profit_factor", 1.00, "holdout_profit_factor"),
    ("pnl", -50.0, "holdout_pnl_positive"),
    ("trades", 50, "holdout_frequency"),
])
def test_holdout_gates_fail_on_each_weak_metric(field, value, gate_name):
    metrics = {"trades": 200, "pnl": 500.0, "profit_factor": 1.20,
               "expectancy_usd": 2.5}
    metrics[field] = value
    gates = tune.holdout_gates(
        {"bars": 1000, "metrics": metrics},
        {"bars": 1000, "metrics": {"trades": 200, "pnl": 0.0}},
        min_trades_per_1000_bars=120.0)
    failed = {gate.name for gate in gates if not gate.passed}
    assert gate_name in failed


def test_holdout_gate_fails_without_beating_baseline():
    winner = {"bars": 1000,
              "metrics": {"trades": 200, "pnl": 100.0,
                          "profit_factor": 1.20, "expectancy_usd": 0.5}}
    baseline = {"bars": 1000,
                "metrics": {"trades": 200, "pnl": 500.0,
                            "profit_factor": 1.10, "expectancy_usd": 2.5}}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=120.0)
    by_name = {gate.name: gate for gate in gates}
    assert not by_name["holdout_beats_baseline"].passed


def test_holdout_gate_rejects_pnl_improvement_from_trading_less():
    # less volume can improve total PnL while making each trade worse
    winner = {"bars": 1000,
              "metrics": {"trades": 100, "pnl": 500.0,
                          "profit_factor": 0.63, "avg_pnl_per_trade": -5.0}}
    baseline = {"bars": 1000,
                "metrics": {"trades": 200, "pnl": 400.0,
                            "profit_factor": 0.64, "avg_pnl_per_trade": -2.0}}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=50.0)
    by_name = {gate.name: gate for gate in gates}
    assert by_name["holdout_frequency"].passed
    assert not by_name["holdout_beats_baseline"].passed


def test_holdout_primary_symbol_gate_requires_xauusd_improvement():
    aggregate = {"trades": 200, "pnl": 500.0,
                 "profit_factor": 1.20, "avg_pnl_per_trade": 2.5}
    base_aggregate = {"trades": 200, "pnl": 100.0,
                      "profit_factor": 1.05, "avg_pnl_per_trade": 0.5}
    winner = {"bars": 1000, "metrics": aggregate,
              "symbols": {"XAUUSD": {"trades": 100, "pnl": -100.0,
                                     "avg_pnl_per_trade": -1.0}}}
    baseline = {"bars": 1000, "metrics": base_aggregate,
                "symbols": {"XAUUSD": {"trades": 100, "pnl": -50.0,
                                       "avg_pnl_per_trade": -0.5}}}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=120.0,
                               primary_symbol="XAUUSD")
    by_name = {gate.name: gate for gate in gates}
    assert not by_name["holdout_primary_symbol"].passed

    winner["symbols"]["XAUUSD"] = {"trades": 100, "pnl": 200.0,
                                   "avg_pnl_per_trade": 2.0}
    baseline["symbols"]["XAUUSD"] = {"trades": 100, "pnl": 100.0,
                                     "avg_pnl_per_trade": 1.0}
    gates = tune.holdout_gates(winner, baseline,
                               min_trades_per_1000_bars=120.0,
                               primary_symbol="XAUUSD")
    by_name = {gate.name: gate for gate in gates}
    assert by_name["holdout_primary_symbol"].passed


# ------------------------------------------------------------- study loop
def _metrics(trades, pnl, pf=1.2, expectancy=1.0):
    return {"trades": trades, "pnl": pnl, "profit_factor": pf,
            "expectancy_usd": expectancy, "win_rate_pct": 50.0,
            "max_drawdown": 0.0, "avg_r": 0.1, "avg_pnl_per_trade": expectancy,
            "wins": max(1, trades // 2), "gross_win": abs(pnl),
            "gross_loss": 1.0, "exits": {}}


def test_holdout_is_evaluated_once_and_cannot_change_the_winner():
    cfg = {
        "agents": {"weight_trend": 1.1, "weight_momentum": 0.9,
                   "weight_reversion": 0.4, "weight_breakout": 1.0,
                   "weight_pullback": 0.6, "consensus_threshold": 0.30},
        "risk": {"dead_market_atr_ratio": 0.62},
    }
    split = tune.build_study_split(1000, warmup_bars=100, tail_bars=10)
    weights = (("trend", 1.1), ("momentum", 0.9), ("reversion", 0.4),
               ("breakout", 1.0), ("pullback", 0.6))
    good = tune.Candidate("good", weights, 0.30, 0.62, "live")
    bad = tune.Candidate("bad", weights, 0.40, 0.62, "live")
    baseline = tune.baseline_candidate(cfg)

    calls = []

    def fake_evaluate(caches, cfg, candidate, window, *, symbols,
                      timeframe, costs):
        calls.append((candidate.label, window))
        bars = window[1] - window[0]
        if window == split.window("train"):
            pnl = 100.0 if candidate.label == good.label else 50.0
        elif window == split.window("validation"):
            pnl = {"good": 500.0, "bad": 200.0,
                   "baseline": 100.0}.get(candidate.preset, 0.0)
        else:                       # holdout: the bad candidate looks amazing
            pnl = {"good": -50.0, "bad": 9_999.0,
                   "baseline": -100.0}.get(candidate.preset, 0.0)
        return {"metrics": _metrics(200, pnl), "bars": bars, "symbols": {}}

    result = tune.run_study(
        {"XAUUSD": {}}, cfg, split, candidates=[good, bad],
        symbols=["XAUUSD"], timeframe="M15", costs={}, top_k=2,
        min_trades_per_1000_bars=100.0, evaluate_fn=fake_evaluate)

    assert result["winner"]["label"] == good.label
    holdout_labels = {label for label, window in calls
                      if window == split.window("holdout")}
    assert holdout_labels == {good.label, baseline.label}
    assert bad.label not in holdout_labels
    holdout_calls = [call for call in calls
                     if call[1] == split.window("holdout")]
    assert len(holdout_calls) == 2
    assert not result["approved"]          # the winner lost on the holdout


def test_run_study_finds_no_winner_when_all_are_too_rare():
    cfg = {"agents": {"consensus_threshold": 0.30},
           "risk": {"dead_market_atr_ratio": 0.62}}
    split = tune.build_study_split(1000, warmup_bars=100, tail_bars=10)
    candidate = tune.baseline_candidate(cfg)

    def fake_evaluate(caches, cfg, candidate, window, *, symbols,
                      timeframe, costs):
        return {"metrics": _metrics(5, 100.0), "bars": window[1] - window[0],
                "symbols": {}}

    result = tune.run_study(
        {"XAUUSD": {}}, cfg, split, candidates=[candidate],
        symbols=["XAUUSD"], timeframe="M15", costs={}, top_k=2,
        min_trades_per_1000_bars=100.0, evaluate_fn=fake_evaluate)
    assert result["winner"] is None
    assert result["holdout"] == {}
    assert not result["approved"]


# -------------------------------------------------------------------- apply
def test_merge_patch_is_deep_and_does_not_mutate():
    cfg = {"agents": {"consensus_threshold": 0.30, "weight_trend": 1.1},
           "risk": {"dead_market_atr_ratio": 0.62}}
    patched = tune.merge_patch(cfg, {"agents": {"consensus_threshold": 0.40}})
    assert patched["agents"]["consensus_threshold"] == pytest.approx(0.40)
    assert cfg["agents"]["consensus_threshold"] == pytest.approx(0.30)


def test_apply_patch_file_validates_before_writing(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agents": {"consensus_threshold": 0.30},
                                "risk": {}}), encoding="utf-8")
    monkeypatch.setattr(tune, "validate_config", lambda cfg: [])
    tune.apply_patch_file(str(path), {"agents": {"consensus_threshold": 0.40}})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["agents"]["consensus_threshold"] == pytest.approx(0.40)
    assert (tmp_path / "config.json.bak_pre_tune").exists()

    monkeypatch.setattr(tune, "validate_config",
                        lambda cfg: ["agents.consensus_threshold: bad"])
    with pytest.raises(SystemExit, match="invalid config"):
        tune.apply_patch_file(str(path), {"agents": {"consensus_threshold": 9}})
    unchanged = json.loads(path.read_text(encoding="utf-8"))
    assert unchanged["agents"]["consensus_threshold"] == pytest.approx(0.40)
