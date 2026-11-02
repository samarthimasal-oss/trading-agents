"""Bar-by-bar backtest of the same agent stack, measured in R-multiples.

Data comes from MT5 (if connected) or a CSV (open,high,low,close required).
The simulation: consensus decides, next bar opens the trade, SL/TP checked on
each bar's range, ATR-trailing at bar close, opposite consensus closes.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from agents import quant
from agents.quant import enrich, to_frame

log = logging.getLogger("backtest")


@dataclass
class BTResult:
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_R: float
    profit_factor: float
    expectancy_R: float
    max_dd_R: float
    max_consec_losses: int
    sharpe: float
    equity_curve_R: list


def fetch_bars_mt5(cfg: dict, symbol: str, bars: int) -> pd.DataFrame:
    from core.mt5_client import MT5Client, MT5Error
    client = MT5Client(cfg["mt5"])
    client.connect()
    try:
        symbol = client.resolve_symbol(symbol)
        raw = client.copy_bars(symbol, cfg["trading"]["timeframe"], bars)
        df = to_frame(raw)
        df["atr"] = quant.atr(df, cfg["agents"]["atr_period"])
        return df
    finally:
        client.shutdown()


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    df = df.rename(columns={cols[k]: k for k in ("open", "high", "low", "close") if k in cols})
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise SystemExit(f"CSV missing column: {col}")
    if "time" in df.columns:
        from market.data_engine import normalize_epoch

        df["time"] = normalize_epoch(df["time"])
        df = df.set_index("time")
    return df


def run(df: pd.DataFrame, cfg: dict, warmup: int = 120) -> BTResult:
    a = cfg["agents"]
    df = enrich(df, a).dropna()
    if len(df) <= warmup:
        raise SystemExit(f"Not enough bars ({len(df)}) after adding indicators (warmup {warmup}).")

    r_cfg = cfg["risk"]
    risk_pct = float(r_cfg["risk_percent_per_trade"])
    sl_mult, tp_mult = float(a["atr_sl_mult"]), float(a["atr_tp_mult"])
    trail = float(r_cfg.get("trailing_atr", 1.0))

    equity = 0.0            # in R units
    curve = [0.0]
    pos = None              # dict: dir, entry, sl, tp, open_bar_idx
    trades: list[float] = []
    consecutive = 0
    max_consec = 0

    closes = df["close"].to_numpy()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    atrs = df["atr"].to_numpy()
    times = df.index.to_numpy()

    for i in range(warmup, len(df)):
        price = closes[i]
        atr_val = atrs[i]

        # --- manage open position with THIS bar's range first -----------------
        if pos:
            d, entry, sl, tp = pos["dir"], pos["entry"], pos["sl"], pos["tp"]
            hi, lo = highs[i], lows[i]
            exit_price = None
            # conservative: assume SL is hit before TP on the same bar
            if d > 0 and lo <= sl:
                exit_price = sl
            elif d < 0 and hi >= sl:
                exit_price = sl
            elif d > 0 and hi >= tp:
                exit_price = tp
            elif d < 0 and lo <= tp:
                exit_price = tp
            if exit_price is not None:
                r_mult = (exit_price - entry) * d / pos["risk_dist"]
                trades.append(r_mult)
                equity += r_mult
                curve.append(equity)
                pos = None
                consecutive = consecutive + 1 if r_mult <= 0 else 0
                max_consec = max(max_consec, consecutive)
            else:
                # trailing stop at bar close
                cand = (price - trail * atr_val) if d > 0 else (price + trail * atr_val)
                if (d > 0 and cand > sl) or (d < 0 and cand < sl):
                    pos["sl"] = cand

        # --- new signal at bar close (indicators are precomputed & causal,
        #     agents only read the last rows, so slicing history is not needed)
        window = df.iloc[max(0, i - 7): i + 1]      # just enough rows for momentum ROC
        cons = quant.run_quant_agents(window, a)
        r_dist = max(atr_val * sl_mult, 1e-9)

        if pos and cons.direction != 0 and pos["dir"] != cons.direction:
            # opposite signal -> exit at this close
            r_mult = (price - pos["entry"]) * pos["dir"] / pos["risk_dist"]
            trades.append(r_mult)
            equity += r_mult
            curve.append(equity)
            pos = None
            consecutive = consecutive + 1 if r_mult <= 0 else 0
            max_consec = max(max_consec, consecutive)

        if pos is None and cons.direction != 0:
            # enter at next bar open (realistic, avoids look-ahead)
            if i + 1 < len(df):
                entry = opens[i + 1]
                sl = entry - r_dist if cons.direction > 0 else entry + r_dist
                tp = entry + atr_val * tp_mult if cons.direction > 0 else entry - atr_val * tp_mult
                pos = {"dir": cons.direction, "entry": entry, "sl": sl, "tp": tp,
                       "risk_dist": r_dist}

    arr = np.array(trades) if trades else np.array([0.0])
    wins = arr[arr > 0]
    losses_arr = arr[arr <= 0]
    pf = (wins.sum() / abs(losses_arr.sum())) if len(wins) and losses_arr.sum() != 0 \
        else (float("inf") if len(wins) else 0.0)
    curve_arr = np.array(curve)
    peak = np.maximum.accumulate(curve_arr)
    max_dd = float((peak - curve_arr).max()) if len(curve_arr) else 0.0
    per_bar = np.diff(curve_arr) / max(len(df) / 50, 1)
    sharpe = float(per_bar.mean() / per_bar.std() * math.sqrt(252)) if per_bar.std() > 0 else 0.0

    return BTResult(
        trades=len(arr), wins=len(wins), losses=len(losses_arr),
        win_rate=round(len(wins) / len(arr) * 100, 1) if len(arr) else 0.0,
        total_R=round(equity, 2),
        profit_factor=round(min(pf, 99.0), 2),
        expectancy_R=round(arr.mean(), 3) if len(arr) else 0.0,
        max_dd_R=round(max_dd, 2),
        max_consec_losses=max_consec,
        sharpe=round(sharpe, 2),
        equity_curve_R=curve,
    )


def print_result(res: BTResult, bar_label: str = "") -> None:
    eq = res.equity_curve_R
    print()
    print("=" * 54)
    print("BACKTEST RESULT (R-multiples, per-trade risk fixed)")
    print("=" * 54)
    print(f"symbol/timeframe data : {bar_label}")
    print(f"trades                : {res.trades}")
    print(f"win rate              : {res.win_rate}%")
    print(f"total P/L             : {res.total_R:+.2f} R   (risk {1:.0f}R/trade)")
    print(f"profit factor         : {res.profit_factor}")
    print(f"expectancy            : {res.expectancy_R:+.3f} R/trade")
    print(f"max drawdown          : {res.max_dd_R:.2f} R")
    print(f"max consecutive losses: {res.max_consec_losses}")
    print(f"sharpe (approx)       : {res.sharpe}")
    # ASCII equity curve
    if len(eq) > 2:
        w = 60
        step = max(1, len(eq) // w)
        pts = eq[::step][:w] + ([eq[-1]] if len(eq) % step else [])
        lo, hi = max(min(pts), -10), max(max(pts), 1)
        span = (hi - lo) or 1
        rows = []
        for row in range(6, -1, -1):
            thresh = lo + (hi - lo) * row / 6
            line = "".join("*" if p >= thresh else " " for p in pts)
            rows.append(f" {thresh:7.1f} |{line}{'*' if pts[-1] >= thresh else ' '}")
        print("equity curve (1R = one full risk unit):")
        print("\n".join(rows))
        print("  " + "-" * (len(rows[0]) - 1))
    print("=" * 54)
    print("Note: fees/swap not modelled; size positions with the RiskAgent.")
