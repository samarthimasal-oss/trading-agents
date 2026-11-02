"""Horizon-edge scan: which agent votes have real edge at which forward horizon?

Computes per-agent confidence-weighted mean(vote_dir * forward_move_in_ATR)
for 2/4/8/16/24-bar horizons, separately on the training window (first 60%)
and the validation window (last 40%). Vectorized numpy — instant.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import backtest as bt                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config.json")


def edges(c: dict, lo: int, hi: int, hors=(2, 4, 8, 16, 24)) -> np.ndarray:
    """mean(vote_dir * confidence * forward_move_in_ATR) per agent per horizon.

    Correct pairing: vote computed at bar i (on data up to i) vs
    (close[i+1+h] - close[i+1]) / atr[i]  -> no lookahead.
    """
    closes = c["df"]["close"].to_numpy()
    atrs = np.maximum(c["df"]["atr"].to_numpy(), 1e-9)
    votes = c["votes"].astype(float)                       # (n, agents)
    confs = c["confs"].astype(float)                       # (n, agents)
    out = []
    for h in hors:
        hi_cap = min(hi, len(closes) - 1 - h)
        idx = np.arange(lo, hi_cap)                         # bars i with forward data
        fwd = (closes[idx + 1 + h] - closes[idx + 1]) / atrs[idx]    # (bars,)
        e = (votes[idx] * confs[idx] * fwd[:, None]).sum(axis=0)     # (agents,)
        e = e / np.maximum(np.sum(xc := confs[idx], axis=0), 1e-9)
        out.append(e)
    return np.asarray(out).T                                # (agents, horizons)


def main():
    cfg = json.load(open(CFG, encoding="utf-8"))
    n_train = 4788
    hors = (2, 4, 8, 16, 24)
    print("agent        " + "".join("%9dh" % h for h in hors) + "   (train | validate)")
    for sym in cfg["trading"]["symbols"]:
        c = bt.load_signals(sym)
        for label, lo, hi in (("train ", 300, n_train), ("valid ", n_train, c["df"].shape[0] - 30)):
            es = edges(c, lo, hi, hors)
            print(f"== {sym} {label} ==")
            for k, name in enumerate(c["agents"]):
                print("  %-9s %s   %s" % (
                    name,
                    " | ".join("%+8.4f" % es[k, j] for j in range(len(hors))),
                    "(ATR units)"))


if __name__ == "__main__":
    main()
