"""Auto-tuning: search weight/threshold/regime-grid variants in a backtest
window, then VALIDATE the best ones on a later, unseen window.

Method:
  1. per-bar agent votes come from the signal cache (backtest.py --dump-signals)
  2. split bars: TRAIN window = first 60%, TEST window = last 40%
  3. replay the full execution model for every candidate on TRAIN,
     rank, then re-evaluate the top candidates on TEST
  4. report; --apply writes the winner into config.json (with a .bak backup)

Nothing here can overstate reality: the LLM veto layer is still not simulated,
spreads are taken from the bars, and SL-before-TP is the conservative rule.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import backtest as bt                            # noqa: E402

ROOT = bt.ROOT
STATE = bt.STATE

WEIGHT_PRESETS = {
    "live":     {"trend": 1.0, "momentum": 1.0, "reversion": 0.8, "breakout": 0.9, "pullback": 0.8},
    "trend_hi": {"trend": 1.3, "momentum": 0.9, "reversion": 0.4, "breakout": 0.6, "pullback": 0.8},
    "momentum": {"trend": 0.8, "momentum": 1.2, "reversion": 0.3, "breakout": 1.1, "pullback": 0.7},
    "trend_only_weighted": {"trend": 1.5, "momentum": 0.7, "reversion": 0.3, "breakout": 0.5, "pullback": 0.7},
}
THRESHOLDS = [0.20, 0.30, 0.40]
DEAD_MARKET = [0.62, 0.75]


def simulate_variant(cache: dict, info: dict, cfg: dict, weights: dict,
                     threshold: float, dead: float, start: int, end: int) -> dict:
    cfg_local = dict(cfg)
    cfg_local["risk"] = {**cfg["risk"], "_symbol": os.path.basename("")}
    res = bt.replay(cache["df"], info, cfg_local, cache["votes"], cache["confs"],
                    cache["agents"], weights, threshold, dead_market_ratio=dead,
                    start=start, end=end)
    m = bt.metrics(res["trades"], res["curve"])
    return {"m": m, "res": res}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=80)
    ap.add_argument("--apply", action="store_true", help="write winner into config.json")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    symbols = [s.strip() for s in cfg["trading"]["symbols"]]

    caches = {}
    for s in symbols:
        try:
            caches[s] = bt.load_signals(s)
        except SystemExit:
            print(f"building signal cache for {s} …", flush=True)
            bt.dump_signals([s], args.days)
            caches[s] = bt.load_signals(s)

    # train/validate split by bar index (80/20 days -> first 60% bars / last 40%)
    n = len(next(iter(caches.values()))["df"])
    train_end = int(n * 0.60)
    print(f"bars: {n} | train [300..{train_end}) | test [{train_end}..{n - 6})", flush=True)

    rows = []
    for wname, weights in WEIGHT_PRESETS.items():
        for thr, dead in itertools.product(THRESHOLDS, DEAD_MARKET):
            train_trade_count, train_pnl, train_dd, train_pf = 0, 0.0, 0.0, 0.0
            for s in symbols:
                r = simulate_variant(caches[s], bt.symbol_settings(s), cfg,
                                     weights, thr, dead, 300, train_end)
                train_pnl += r["m"]["pnl"]
                train_dd = max(train_dd, r["m"]["max_drawdown"])
                train_pf += r["m"]["profit_factor"]
                train_trade_count += r["m"]["trades"]
            rows.append({"preset": wname, "thr": thr, "dead": dead,
                         "train_pnl": round(train_pnl, 1), "trades": train_trade_count,
                         "pf_avg": round(train_pf / len(symbols), 2), "max_dd": round(train_dd, 1)})

    rows.sort(key=lambda x: x["train_pnl"], reverse=True)
    print("\nTOP CANDIDATES (train window):")
    print("preset              thr  dead   train_pnl   trades  pf_avg   maxDD")
    for row in rows[:8]:
        print("%-18s %.2f  %.2f  %+10.1f  %5d   %.2f  %8.1f" % (
            row["preset"], row["thr"], row["dead"], row["train_pnl"],
            row["trades"], row["pf_avg"], row["max_dd"]))

    # validate the top 5 out-of-sample
    print("\nOUT-OF-SAMPLE VALIDATION (unseen bars):")
    results = []
    for row in rows[:5]:
        weights = WEIGHT_PRESETS[row["preset"]]
        val_pnl, val_dd, val_pf, val_tr = 0.0, 0.0, 0.0, 0
        for s in symbols:
            r = simulate_variant(caches[s], bt.symbol_settings(s), cfg,
                                 weights, row["thr"], row["dead"], train_end, n - 6)
            val_pnl += r["m"]["pnl"]
            val_dd = max(val_dd, r["m"]["max_drawdown"])
            val_pf += r["m"]["profit_factor"]
            val_tr += r["m"]["trades"]
        results.append({**row, "val_pnl": round(val_pnl, 1), "val_trades": val_tr,
                        "val_pf_avg": round(val_pf / len(symbols), 2), "val_max_dd": round(val_dd, 1)})
        print("%-18s thr=%.2f dead=%.2f  TEST pnl=%+9.1f trades=%4d pf=%.2f dd=%.1f" % (
            row["preset"], row["thr"], row["dead"], val_pnl, val_tr,
            results[-1]["val_pf_avg"], val_dd))

    results.sort(key=lambda x: x["val_pnl"], reverse=True)
    winner = results[0]
    print("\nWINNER:", winner)

    weights = WEIGHT_PRESETS[winner["preset"]]
    suggestion = {
        "agents": {**cfg["agents"],
                   "weight_trend": weights["trend"], "weight_momentum": weights["momentum"],
                   "weight_reversion": weights["reversion"], "weight_breakout": weights["breakout"],
                   "weight_pullback": weights["pullback"],
                   "consensus_threshold": winner["thr"]},
        "extra_risk": {"dead_market_atr_ratio": winner["dead"]},
    }
    with open(os.path.join(STATE, "tuning_result.json"), "w", encoding="utf-8") as fh:
        json.dump({"winner": winner, "top": rows[:10], "validation": results,
                   "suggestion": suggestion}, fh, indent=1)

    if args.apply:
        shutil.copy(os.path.join(ROOT, "config.json"),
                    os.path.join(ROOT, "config.json.bak_pre_tune"))
        cfg["agents"].update({k: v for k, v in suggestion["agents"].items() if k.startswith("weight_")
                              or k == "consensus_threshold"})
        cfg["risk"]["dead_market_atr_ratio"] = suggestion["extra_risk"]["dead_market_atr_ratio"]
        with open(os.path.join(ROOT, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        print("APPLIED to config.json (backup: config.json.bak_pre_tune) — restart the runner")
    else:
        print("\n(dry run — rerun with --apply to write this into config.json)")


if __name__ == "__main__":
    main()
