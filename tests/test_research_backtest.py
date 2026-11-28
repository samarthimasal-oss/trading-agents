"""Research backtest realism: causal fills, costs, sizing and metrics.

These tests pin the properties that make a research number trustworthy:
decisions fill on the *next* bar, buys pay the ask, gaps fill at the gap open,
the stop wins intrabar ties, commission/slippage are charged, volume is
rounded down (never up into over-risk), and metrics are derived from trades.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from research import backtest as bt


# --------------------------------------------------------------------- fixtures
XAU_INFO = {
    "digits": 2,
    "point": 0.01,
    "trade_tick_size": 0.01,
    "trade_tick_value": 1.0,
    "trade_contract_size": 100.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "stops_level": 0,
    "freezing_level": 0,
}

EUR_INFO = {
    "digits": 5,
    "point": 0.00001,
    "trade_tick_size": 0.00001,
    "trade_tick_value": 1.0,
    "trade_contract_size": 100_000.0,
    "volume_min": 0.01,
    "volume_max": 500.0,
    "volume_step": 0.01,
    "stops_level": 0,
    "freezing_level": 0,
}


def make_cfg(**risk_overrides) -> dict:
    risk = {
        "risk_percent_per_trade": 0.5,
        "fixed_lot": 1.0,
        "max_notional_leverage_per_position": 0.0,
        "breakeven_atr": 1000.0,     # keep the stop where the test put it
        "trailing_atr": 1000.0,
        "trailing_step_atr": 0.0,
        "partial_close_enabled": False,
        "partial_close_at_r": 1.0,
        "partial_close_pct": 50.0,
        "time_stop_minutes": 0.0,
        "min_gap_seconds_between_same_symbol_trades": 0,
    }
    risk.update(risk_overrides)
    return {
        "agents": {"atr_sl_mult": 1.5, "atr_tp_mult": 3.0},
        "risk": risk,
        "backtest": {},
    }


def frame(opens, highs, lows, closes, spread=20.0, atr=2.0) -> pd.DataFrame:
    n = len(opens)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-05 00:00", periods=n, freq="15min"),
        "open": [float(x) for x in opens],
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "spread": [float(spread)] * n,
        "atr": [float(atr)] * n,
    })


def votes_at(n: int, index: int = 2, direction: int = 1):
    votes = np.zeros((n, 1), dtype=np.int8)
    confs = np.zeros((n, 1), dtype=np.float32)
    votes[index, 0] = direction
    confs[index, 0] = 1.0
    return votes, confs


def run(frame_: pd.DataFrame, *, start=2, symbol="XAUUSD", info=None,
        cfg=None, commission=0.0, slippage=0.0, floor=0.0,
        direction=1, timeframe="M15", end=None) -> dict:
    n = len(frame_)
    votes, confs = votes_at(n, index=start, direction=direction)
    return bt.replay(
        frame_, info or XAU_INFO, cfg or make_cfg(), votes, confs, ["trend"],
        {"trend": 1.0}, 0.3, start=start,
        end=n if end is None else end, symbol=symbol, timeframe=timeframe,
        commission_per_lot=commission, slippage_points=slippage,
        spread_floor_points=floor, bars_history=start,
        start_equity=100_000.0,
    )


def entry_frame() -> pd.DataFrame:
    return frame(
        opens=[2000, 2000, 2000, 2010, 2010.5, 2010.5],
        highs=[2000.1, 2000.1, 2000.1, 2011, 2011, 2011],
        lows=[1999.9, 1999.9, 1999.9, 2009, 2010, 2010],
        closes=[2000, 2000, 2000, 2010.5, 2010.5, 2010.5],
    )


# ------------------------------------------------------------------ execution
def test_signal_fills_at_next_bar_open_not_the_signal_close():
    res = run(entry_frame())
    assert len(res["trades"]) == 1
    trade = res["trades"][0]
    # decided on bar 2 (close 2000), filled on bar 3's open (2010) + ask spread
    assert trade["entry_bar"] == 3
    assert trade["entry"] == pytest.approx(2010.0 + 0.20)
    assert trade["time"] == str(entry_frame()["time"].iloc[2])
    assert trade["entry_time"] == str(entry_frame()["time"].iloc[3])


def test_long_pays_ask_and_exit_pays_adverse_slippage():
    res = run(entry_frame(), slippage=2.0)      # 2 points = 0.02
    trade = res["trades"][0]
    assert trade["entry"] == pytest.approx(2010.0 + 0.20 + 0.02)
    # long exits on the bid at the last close (2010.5) minus exit slippage
    assert trade["exit_price"] == pytest.approx(2010.5 - 0.02)
    assert trade["exit_why"] == "eod"


def test_gap_through_stop_fills_at_gap_open_not_the_stop_level():
    data = entry_frame()
    data.loc[4, ["open", "high", "low", "close"]] = [2000.0, 2001.0, 1999.0, 2000.0]
    res = run(data)
    trade = res["trades"][0]
    assert trade["exit_why"] == "sl_gap"
    assert trade["exit_bar"] == 4
    assert trade["exit_price"] == pytest.approx(2000.0)   # worse than SL 2007.2
    assert trade["pnl"] == pytest.approx((2000.0 - 2010.2) * 100.0)


def test_stop_is_assumed_before_target_when_one_bar_touches_both():
    data = entry_frame()
    data.loc[4, ["open", "high", "low", "close"]] = [2010.0, 2020.0, 2005.0, 2010.0]
    res = run(data)
    trade = res["trades"][0]
    assert trade["exit_why"] == "sl"
    assert trade["exit_price"] == pytest.approx(2007.2)


def test_target_fills_when_only_the_target_is_touched():
    data = entry_frame()
    data.loc[4, ["open", "high", "low", "close"]] = [2010.0, 2017.0, 2009.0, 2016.0]
    res = run(data)
    trade = res["trades"][0]
    assert trade["exit_why"] == "tp"
    assert trade["exit_price"] == pytest.approx(2016.2)


def test_spread_floor_is_used_when_recorded_spread_is_zero():
    data = entry_frame()
    data["spread"] = 0.0
    res = run(data, floor=20.0)
    assert res["trades"][0]["entry"] == pytest.approx(2010.0 + 0.20)
    assert res["costs"]["spread_floor_points"] == 20.0


def test_commission_is_a_round_turn_cost_not_a_per_side_surprise():
    free = run(entry_frame(), commission=0.0)
    paid = run(entry_frame(), commission=7.0)
    assert free["trades"][0]["exit_why"] == paid["trades"][0]["exit_why"]
    assert paid["trades"][0]["commission"] == pytest.approx(7.0)
    assert (free["trades"][0]["pnl"] - paid["trades"][0]["pnl"]
            == pytest.approx(7.0))
    assert paid["trades"][0]["r_multiple"] < free["trades"][0]["r_multiple"]


def test_short_entry_sells_the_bid_and_exits_at_the_ask():
    data = frame(
        opens=[2000, 2000, 2000, 1990, 1989.5, 1989.5],
        highs=[2000.1, 2000.1, 2000.1, 1991, 1990, 1990],
        lows=[1999.9, 1999.9, 1999.9, 1989, 1989, 1989],
        closes=[2000, 2000, 2000, 1989.5, 1989.5, 1989.5],
    )
    res = run(data, direction=-1)
    trade = res["trades"][0]
    assert trade["side"] == -1
    assert trade["entry"] == pytest.approx(1990.0)          # sold the bid
    # short TP = 1990 - 6 = 1984 (not touched); end-of-data buy-back at ask
    assert trade["exit_price"] == pytest.approx(1989.5 + 0.20)
    assert trade["exit_why"] == "eod"


def test_no_future_bar_can_change_an_already_closed_trade():
    n = 20
    opens = [2000.0] * n
    highs = [2000.1] * n
    lows = [1999.9] * n
    closes = [2000.0] * n
    opens[3], highs[3], lows[3], closes[3] = 2010.0, 2011.0, 2009.0, 2010.5
    for index in (4, 5, 7, 8, 9, 10, 11):
        opens[index], highs[index] = 2010.0, 2011.0
        lows[index], closes[index] = 2009.0, 2010.5
    opens[6], highs[6], lows[6], closes[6] = 2010.0, 2020.0, 2009.0, 2016.0
    base = frame(opens, highs, lows, closes)
    modified = base.copy()
    modified.loc[12:, ["open", "high", "low", "close"]] = 1500.0

    first = run(base, end=n)
    second = run(modified, end=n)
    assert first["trades"][0]["exit_why"] == "tp"
    closed = [t for t in first["trades"] if int(t["exit_bar"]) < 12]
    assert closed
    assert closed == [t for t in second["trades"]
                      if int(t["exit_bar"]) < 12]


def test_next_bar_fill_is_not_managed_with_the_pre_entry_close():
    """A signal-bar close ahead of the next open must not move the new stop."""
    data = frame(
        opens=[2000, 2000, 2000, 2000, 2000],
        highs=[2000.1, 2000.1, 2011, 2001, 2000.5],
        lows=[1999.9, 1999.9, 1999, 1999, 1999.5],
        closes=[2000, 2000, 2010, 2000, 2000],
    )
    res = run(data, cfg=make_cfg(breakeven_atr=0.1), start=2, end=5)
    trade = res["trades"][0]
    assert trade["entry_bar"] == 3
    assert trade["entry"] == pytest.approx(2000.20)
    # the real stop is 1997.20; bar 3's low (1999.00) must not hit it and the
    # stale 2010.00 close must not have moved it to breakeven either
    assert trade["exit_why"] == "eod"
    assert trade["exit_bar"] == 4
    assert trade["exit_price"] == pytest.approx(2000.0)


# ------------------------------------------------------------------ management
def partial_frame() -> pd.DataFrame:
    return frame(
        opens=[2000, 2000, 2000, 2010, 2011],
        highs=[2000.1, 2000.1, 2000.1, 2015, 2012],
        lows=[1999.9, 1999.9, 1999.9, 2013, 2010],
        closes=[2000, 2000, 2000, 2014, 2011],
    )


def test_partial_close_books_half_at_one_r_and_forces_breakeven():
    res = run(partial_frame(), start=2, end=5,
              cfg=make_cfg(fixed_lot=1.0, partial_close_enabled=True))
    trade = res["trades"][0]
    assert trade["volume"] == pytest.approx(1.0)
    assert trade["open_volume"] == pytest.approx(0.5)
    assert len(trade["partials"]) == 1
    partial = trade["partials"][0]
    assert partial["volume"] == pytest.approx(0.5)
    assert partial["price"] == pytest.approx(2014.0)
    assert partial["pnl"] == pytest.approx(190.0)
    # the forced breakeven stop (entry + min_dist + spread) is hit on bar 4
    assert trade["exit_why"] == "sl"
    assert trade["exit_price"] == pytest.approx(2010.42)
    assert trade["pnl"] == pytest.approx(201.0)
    assert trade["commission"] == pytest.approx(0.0)


def test_partial_close_charges_commission_on_both_legs():
    res = run(partial_frame(), start=2, end=5, commission=7.0,
              cfg=make_cfg(fixed_lot=1.0, partial_close_enabled=True))
    trade = res["trades"][0]
    assert trade["partials"][0]["commission"] == pytest.approx(3.5)
    assert trade["commission"] == pytest.approx(7.0)
    assert trade["pnl"] == pytest.approx(194.0)


def test_trailing_step_blocks_small_improvements():
    pos = {"side": 1, "entry": 2000.0, "sl": 1997.0}
    cfg = {"trailing_atr": 1.0, "breakeven_atr": 1000.0,
           "trailing_step_atr": 1.0}
    bt._apply_manage(pos, 2000.5, 2.0, 0.20, 0.02, cfg)
    assert pos["sl"] == pytest.approx(1997.0)   # 1.5 < step 2.0
    bt._apply_manage(pos, 2001.0, 2.0, 0.20, 0.02, cfg)
    assert pos["sl"] == pytest.approx(1999.0)   # exactly one step


def test_time_stop_closes_after_the_configured_minutes():
    data = frame(
        opens=[2000, 2000, 2000, 2010, 2011],
        highs=[2000.1, 2000.1, 2000.1, 2011.5, 2011.5],
        lows=[1999.9, 1999.9, 1999.9, 2010.5, 2010.5],
        closes=[2000, 2000, 2000, 2011, 2011],
    )
    res = run(data, start=2, end=5,
              cfg=make_cfg(fixed_lot=1.0, time_stop_minutes=30))
    trade = res["trades"][0]
    assert trade["entry_bar"] == 3
    assert trade["exit_bar"] == 4
    assert trade["exit_why"] == "time_stop"
    assert trade["exit_price"] == pytest.approx(2011.0)
    assert trade["pnl"] == pytest.approx(80.0)


def test_partial_volume_floors_to_step_and_keeps_the_position_open():
    assert bt.partial_volume(1.0, XAU_INFO, 50.0) == pytest.approx(0.5)
    assert bt.partial_volume(0.03, XAU_INFO, 50.0) == pytest.approx(0.01)
    assert bt.partial_volume(0.01, XAU_INFO, 50.0) == 0.0
    assert bt.partial_volume(0.02, XAU_INFO, 100.0) == 0.0


# ---------------------------------------------------------------------- sizing
def test_size_volume_rounds_down_to_the_broker_step():
    cfg = make_cfg(fixed_lot=0.0, max_notional_leverage_per_position=0.0)
    # 0.5% of 10,000 = $50 risk; $0.30 stop / 0.01 tick * $1 = $30 per lot
    volume = bt.size_volume("XAUUSD", XAU_INFO, 10_000.0, 0.2, 2000.0,
                            cfg["agents"], cfg["risk"], sl_distance=0.30)
    assert volume == pytest.approx(1.66)          # 1.666... floored to 0.01


def test_size_volume_rejects_a_min_lot_that_would_exceed_the_budget():
    cfg = make_cfg(fixed_lot=0.0)
    info = {**XAU_INFO, "volume_min": 0.10}
    # 0.5% of $100 = $0.50 risk, but the minimum 0.10 lot loses $3 at this stop
    volume = bt.size_volume("XAUUSD", info, 100.0, 0.2, 2000.0,
                            cfg["agents"], cfg["risk"], sl_distance=0.30)
    assert volume == 0.0


def test_fixed_lot_is_capped_by_the_risk_budget():
    cfg = make_cfg(fixed_lot=5.0)
    # max_by_risk = 500 / 300 = 1.666 -> 1.66 lots, never the fixed 5
    volume = bt.size_volume("XAUUSD", XAU_INFO, 100_000.0, 2.0, 2000.0,
                            cfg["agents"], cfg["risk"], sl_distance=3.0)
    assert volume == pytest.approx(1.66)


def test_size_volume_uses_the_actual_stop_and_commission():
    cfg = make_cfg(fixed_lot=0.0)
    without = bt.size_volume("XAUUSD", XAU_INFO, 100_000.0, 2.0, 2000.0,
                             cfg["agents"], cfg["risk"], sl_distance=3.0)
    with_comm = bt.size_volume("XAUUSD", XAU_INFO, 100_000.0, 2.0, 2000.0,
                               cfg["agents"], cfg["risk"], sl_distance=3.0,
                               commission_per_lot=7.0)
    assert with_comm < without
    # $500 / (3.0/0.01*$1 + $7) = 1.628... -> 1.62
    assert with_comm == pytest.approx(1.62)


# ------------------------------------------------------------------- timeframes
def test_cooldown_never_shrinks_the_live_second_based_gap():
    assert bt.cooldown_bars(0, "M5") == 0
    assert bt.cooldown_bars(900, "M15") == 1
    assert bt.cooldown_bars(900, "M5") == 3
    assert bt.cooldown_bars(901, "M15") == 2
    assert bt.cooldown_bars(300, "M15") == 1


def test_cache_paths_separate_m5_from_the_legacy_m15_names(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "STATE", str(tmp_path))
    assert bt.bars_path("XAUUSD", "M15").endswith("bt_XAUUSD.csv")
    assert bt.bars_path("XAUUSD", "M5").endswith("bt_XAUUSD_M5.csv")
    assert bt.signals_path("XAUUSD", "M15").endswith("bt_sig_XAUUSD.pkl")
    assert bt.signals_path("XAUUSD", "M5").endswith("bt_sig_XAUUSD_M5.pkl")
    path = tmp_path / "bt_XAUUSD_M5.csv"
    frame([2000] * 8, [2000] * 8, [2000] * 8, [2000] * 8).to_csv(
        path, index=False)
    loaded = bt.load_bars("XAUUSD", days=1, timeframe="M5", offline=True)
    assert len(loaded) == 8
    with pytest.raises(SystemExit):
        bt.load_bars("EURUSD", days=1, timeframe="M5", offline=True)


# --------------------------------------------------------------------- metrics
def test_metrics_report_streaks_sides_months_and_costs():
    trades = [
        {"pnl": 30.0, "pnl_gross": 37.0, "commission": 7.0,
         "r_multiple": 1.0, "side": 1, "exit_bar": 2,
         "exit_time": "2026-01-15 10:00", "bars_held": 4},
        {"pnl": -10.0, "pnl_gross": -3.0, "commission": 7.0,
         "r_multiple": -0.5, "side": 1, "exit_bar": 4,
         "exit_time": "2026-01-20 11:00", "bars_held": 2},
        {"pnl": -5.0, "pnl_gross": -5.0, "commission": 0.0,
         "r_multiple": -0.25, "side": -1, "exit_bar": 6,
         "exit_time": "2026-02-02 09:00", "bars_held": 3},
        {"pnl": 20.0, "pnl_gross": 27.0, "commission": 7.0,
         "r_multiple": 1.0, "side": -1, "exit_bar": 8,
         "exit_time": "2026-02-10 12:00", "bars_held": 5},
    ]
    m = bt.metrics(trades, [100, 100, 130, 130, 120, 120, 115, 115, 135],
                   timeframe="M15", start_equity=100.0)
    assert m["trades"] == 4
    assert m["win_rate_pct"] == 50.0
    assert m["pnl"] == pytest.approx(35.0)
    assert m["gross_pnl"] == pytest.approx(56.0)
    assert m["commission_paid"] == pytest.approx(21.0)
    assert m["profit_factor"] == pytest.approx(3.33)
    assert m["expectancy_usd"] == pytest.approx(8.75)
    assert m["expectancy_r"] == pytest.approx(0.3125, abs=1e-3)
    assert m["max_win_streak"] == 1
    assert m["max_loss_streak"] == 2
    assert m["avg_bars_held"] == pytest.approx(3.5)
    assert m["max_drawdown"] == pytest.approx(15.0)
    assert m["final_equity"] == pytest.approx(135.0)
    assert m["return_pct"] == pytest.approx(35.0)
    assert m["long"] == {"trades": 2, "wins": 1, "win_rate_pct": 50.0,
                         "pnl": 20.0, "avg_r": 0.25}
    assert m["short"] == {"trades": 2, "wins": 1, "win_rate_pct": 50.0,
                          "pnl": 15.0, "avg_r": 0.375}
    assert m["monthly"] == {"2026-01": 20.0, "2026-02": 15.0}
    assert m["bars_per_year"] == 96 * 252


def test_metrics_annualise_sharpe_per_timeframe_and_handle_no_trades():
    m15 = bt.metrics([], [100.0] * 10, timeframe="M15")
    m5 = bt.metrics([], [100.0] * 10, timeframe="M5")
    assert m15["trades"] == 0 and m15["expectancy_r"] is None
    assert m15["bars_per_year"] == 96 * 252
    assert m5["bars_per_year"] == 288 * 252
    assert m15["max_drawdown"] == 0.0


def test_metrics_output_is_json_serialisable():
    m = bt.metrics([], [100.0, 101.0], timeframe="M15", start_equity=100.0)
    payload = json.dumps(m)
    assert json.loads(payload)["timeframe"] == "M15"


def test_symbol_agent_scores_ignore_consensus_rows():
    samples = [
        ("XAUUSD", "t0", "consensus", 1, 0.8, 0.0, 0),
        ("XAUUSD", "t0", "trend", 1, 0.5, 0.5, 1),
        ("XAUUSD", "t0", "momentum", -1, 0.4, -0.3, -1),
        ("XAUUSD", "t0", "trend", 0, 0.2, 0.1, 0),
    ]
    scores = bt.symbol_agent_scores("XAUUSD", samples)
    assert set(scores) == {"trend", "momentum"}
    assert scores["trend"]["n"] == 1
    assert scores["trend"]["hit_pct"] == 100.0
    assert scores["momentum"]["edge_atr"] == 0.3
