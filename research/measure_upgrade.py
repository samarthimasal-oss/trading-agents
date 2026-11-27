"""Before/after measurement for the realistic-backtest upgrade.

The baseline is the research engine as it stood at commit ``a88a920`` (phase 3),
replayed with its ``risk._symbol`` convention.  The new engine is imported from
this tree and receives the symbol/timeframe explicitly.

Both variants replay the **same cached bars and agent votes**, so the only
difference is engine semantics (fills, gap handling, cooldown, sizing,
costs).  Nothing here re-runs the agents or downloads data.

Variants per symbol:
  baseline                      old engine, recorded spread, no commission
  new_zero_cost                 new engine, recorded spread, no commission
  new_zero_cost_same_cooldown   same, with the old 5-bar M15 cooldown forced
  new_with_costs                new engine + spread floor, commission, slippage

Costs are explicit assumptions, not realised broker fills:
  commission = round-turn per lot (default $7)
  slippage   = adverse points per fill (default 2)
  spread     = floor applied when the recorded spread is lower (defaults below)

Usage:
  python -m research.measure_upgrade [--baseline-ref a88a920]
                                     [--symbols XAUUSD,EURUSD]
                                     [--timeframe M15]
                                     [--out state/upgrade_measurement.json]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import time

import numpy as np

from research import backtest as bt

DEFAULT_COSTS = {
    "commission_per_lot_round_turn": 7.0,
    "slippage_points": 2.0,
    "spread_points": 8.0,
    "spread_points_by_symbol": {
        "XAUUSD": 20.0, "EURUSD": 8.0, "GBPUSD": 10.0, "USDJPY": 8.0,
    },
}


def load_baseline(ref: str):
    """Import the research engine from a git revision without touching HEAD."""
    source = subprocess.check_output(
        ["git", "show", f"{ref}:research/backtest.py"], cwd=bt.ROOT,
        text=True, encoding="utf-8")
    path = os.path.join(bt.STATE, f"_baseline_backtest_{ref}.py")
    os.makedirs(bt.STATE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    spec = importlib.util.spec_from_file_location(f"baseline_bt_{ref}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cost_settings(cfg: dict, overrides: argparse.Namespace) -> dict:
    base = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    if overrides.commission is not None:
        base["commission_per_lot_round_turn"] = overrides.commission
    if overrides.slippage is not None:
        base["slippage_points"] = overrides.slippage
    if overrides.spread is not None:
        base["spread_points"] = overrides.spread
    return base


def floor_for(settings: dict, symbol: str) -> float:
    return float((settings.get("spread_points_by_symbol") or {}).get(
        symbol, settings.get("spread_points", 0.0)) or 0.0)


def summarize(rows: dict[str, dict]) -> dict:
    trades = sum(row["trades"] for row in rows.values())
    pnl = sum(row["pnl"] for row in rows.values())
    wins = sum(row["wins"] for row in rows.values())
    gross_win = sum(row["gross_win"] for row in rows.values())
    gross_loss = sum(row["gross_loss"] for row in rows.values())
    return {
        "trades": trades,
        "pnl": round(pnl, 2),
        "win_rate_pct": round(wins / trades * 100, 1) if trades else 0.0,
        "profit_factor": (round(gross_win / gross_loss, 2)
                          if gross_loss > 0 else (99.0 if gross_win else 0.0)),
        "avg_pnl_per_trade": round(pnl / trades, 2) if trades else 0.0,
        "max_drawdown_sum_per_symbol": round(
            sum(row["max_drawdown"] for row in rows.values()), 2),
        "note": ("each symbol was replayed on its own 100k account; the "
                 "drawdown is a sum of per-symbol figures, not a portfolio "
                 "equity curve"),
    }


def row_from(metrics: dict, trades: list[dict]) -> dict:
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    return {
        "trades": metrics["trades"],
        "pnl": metrics["pnl"],
        "win_rate_pct": metrics["win_rate_pct"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "avg_r": metrics.get("avg_r"),
        "avg_pnl_per_trade": (round(metrics["pnl"] / metrics["trades"], 2)
                              if metrics["trades"] else 0.0),
        "wins": sum(1 for p in pnls if p > 0),
        "gross_win": sum(p for p in pnls if p > 0),
        "gross_loss": abs(sum(p for p in pnls if p <= 0)),
        "exits": _exits(trades),
    }


def _exits(trades: list[dict]) -> dict:
    out: dict[str, int] = {}
    for trade in trades:
        key = str(trade.get("exit_why") or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def fill_bias(symbol: str, trades: list[dict], df, info: dict) -> dict:
    """How much the old engine's stop/target fills beat the bar open.

    The baseline checked only high/low, so a stop above the bar open (long) or
    below it (short) was still filled at the stop.  That is not achievable in
    the market; this quantifies the overstatement in USD.
    """
    opens = df["open"].to_numpy(dtype=float)
    spreads = (df["spread"].to_numpy(dtype=float) * info["point"]
               if "spread" in df.columns else np.zeros(len(df)))
    stop_bias = target_bias = 0.0
    stop_count = target_count = 0
    for trade in trades:
        bar = int(trade["exit_bar"])
        vol = float(trade["volume"])
        open_px = float(opens[bar])
        ask_open = open_px + float(spreads[bar])
        side = int(trade["side"])
        why = str(trade["exit_why"])
        old = float(trade["exit_price"])
        if side == 1:
            if why == "sl" and old > open_px:
                stop_bias += bt._settle(symbol, open_px, old, 1, vol, info)
                stop_count += 1
            elif why == "tp" and old < open_px:
                target_bias += bt._settle(symbol, old, open_px, 1, vol, info)
                target_count += 1
        else:
            if why == "sl" and old < ask_open:
                stop_bias += bt._settle(symbol, old, ask_open, 1, vol, info)
                stop_count += 1
            elif why == "tp" and old > ask_open:
                target_bias += bt._settle(symbol, ask_open, old, 1, vol, info)
                target_count += 1
    return {
        "stops_filled_better_than_open": stop_count,
        "stop_overstatement_usd": round(stop_bias, 2),
        "targets_filled_worse_than_open": target_count,
        "target_understatement_usd": round(target_bias, 2),
        "note": ("baseline checked high/low only; a stop beyond the bar open "
                 "was still filled at the stop level. Positive bias = the old "
                 "P/L was too good."),
    }


def replay_current(cache: dict, cfg: dict, weights: dict, symbol: str,
                   timeframe: str, **kwargs) -> dict:
    return bt.replay(
        cache["df"], bt.symbol_settings(symbol), cfg, cache["votes"],
        cache["confs"], cache["agents"], weights,
        float(cfg["agents"]["consensus_threshold"]), start=300,
        end=len(cache["df"]) - 6, symbol=symbol, timeframe=timeframe, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="a88a920")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--commission", type=float, default=None)
    parser.add_argument("--slippage", type=float, default=None)
    parser.add_argument("--spread", type=float, default=None)
    parser.add_argument("--out", default=os.path.join(
        bt.STATE, "upgrade_measurement.json"))
    args = parser.parse_args()

    cfg = json.load(open(os.path.join(bt.ROOT, "config.json"), encoding="utf-8"))
    timeframe = str(args.timeframe or cfg["trading"].get("timeframe") or "M15")
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               or [s.upper() for s in cfg["trading"]["symbols"]])
    baseline_bt = load_baseline(args.baseline_ref)
    settings = cost_settings(cfg, args)
    weights = {name: float(cfg["agents"].get(f"weight_{name}", 1.0))
               for name in [ag.name for ag in bt.quant.AGENTS]}

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "baseline_ref": args.baseline_ref,
        "timeframe": timeframe,
        "symbols": symbols,
        "cost_assumptions": {
            "commission_per_lot_round_turn": settings[
                "commission_per_lot_round_turn"],
            "slippage_points": settings["slippage_points"],
            "spread_floor_points": settings["spread_points"],
            "spread_floor_points_by_symbol": settings[
                "spread_points_by_symbol"],
            "note": ("floor is a minimum charged on top of the recorded bar "
                     "spread; the CSV spread is already broker data. These are "
                     "assumptions, not realised fills."),
        },
        "results": {},
        "aggregate": {},
    }

    for symbol in symbols:
        try:
            cache = bt.load_signals(symbol, timeframe=timeframe)
        except SystemExit:
            print(f"skip {symbol}: no cached signals for {timeframe}")
            continue
        df = cache["df"]
        info = bt.symbol_settings(symbol)
        base_cfg = json.loads(json.dumps(cfg))
        base_cfg["risk"]["_symbol"] = symbol
        baseline = baseline_bt.replay(
            df, info, base_cfg, cache["votes"], cache["confs"],
            cache["agents"], weights,
            float(cfg["agents"]["consensus_threshold"]),
            start=300, end=len(df) - 6)
        baseline_m = baseline_bt.metrics(baseline["trades"], baseline["curve"])

        zero = replay_current(cache, cfg, weights, symbol, timeframe,
                              commission_per_lot=0.0, slippage_points=0.0,
                              spread_floor_points=0.0)
        zero_m = bt.metrics(zero["trades"], zero["curve"], timeframe=timeframe,
                            start_equity=zero["start_equity"])

        same_cfg = json.loads(json.dumps(cfg))
        same_cfg["risk"]["min_gap_seconds_between_same_symbol_trades"] = (
            bt.timeframe_seconds(timeframe) * 5)      # old int(300 // 60) = 5 bars
        same = replay_current(cache, same_cfg, weights, symbol, timeframe,
                              commission_per_lot=0.0, slippage_points=0.0,
                              spread_floor_points=0.0)
        same_m = bt.metrics(same["trades"], same["curve"], timeframe=timeframe,
                            start_equity=same["start_equity"])

        costs = replay_current(
            cache, cfg, weights, symbol, timeframe,
            commission_per_lot=settings["commission_per_lot_round_turn"],
            slippage_points=settings["slippage_points"],
            spread_floor_points=floor_for(settings, symbol))
        costs_m = bt.metrics(costs["trades"], costs["curve"], timeframe=timeframe,
                             start_equity=costs["start_equity"])

        report["results"][symbol] = {
            "bars": int(len(df)), "data": {
                "first": str(df["time"].iloc[0]),
                "last": str(df["time"].iloc[-1])},
            "baseline": row_from(baseline_m, baseline["trades"]),
            "baseline_fill_bias": fill_bias(
                symbol, baseline["trades"], df, info),
            "new_zero_cost": row_from(zero_m, zero["trades"]),
            "new_zero_cost_same_cooldown": row_from(same_m, same["trades"]),
            "new_with_costs": row_from(costs_m, costs["trades"]),
        }
        print(f"\n{symbol}  {len(df)} bars  ({df['time'].iloc[0]} -> "
              f"{df['time'].iloc[-1]})")
        for label, row in report["results"][symbol].items():
            if not isinstance(row, dict) or "trades" not in row:
                continue
            print("  %-30s trades=%5d  pnl=%+10.2f  win=%5.1f%%  pf=%5.2f "
                  " dd=%9.2f  exits=%s"
                  % (label, row["trades"], row["pnl"], row["win_rate_pct"],
                     row["profit_factor"], row["max_drawdown"], row["exits"]))
        bias = report["results"][symbol]["baseline_fill_bias"]
        print("  baseline fills better than the bar open: %d stops, "
              "+$%.2f overstatement; %d targets, -$%.2f understatement"
              % (bias["stops_filled_better_than_open"],
                 bias["stop_overstatement_usd"],
                 bias["targets_filled_worse_than_open"],
                 bias["target_understatement_usd"]))

    labels = ["baseline", "new_zero_cost", "new_zero_cost_same_cooldown",
              "new_with_costs"]
    report["aggregate"] = {
        label: summarize({
            symbol: payload[label]
            for symbol, payload in report["results"].items()
            if label in payload})
        for label in labels}
    print("\nAGGREGATE")
    for label in labels:
        agg = report["aggregate"][label]
        print("  %-30s trades=%5d  pnl=%+10.2f  win=%5.1f%%  pf=%5.2f "
              " avg/trade=%+7.2f"
              % (label, agg["trades"], agg["pnl"], agg["win_rate_pct"],
                 agg["profit_factor"], agg["avg_pnl_per_trade"]))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, default=str)
    print(f"\nmeasurement -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
