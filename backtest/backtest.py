"""Backtest engine + signal cache for the TradingAgents pipeline.

Replays the live pipeline over MT5 M15 history:
  * same agent code path (quant.AGENTS), same consensus math
  * same risk sizing (1% risk, leverage cap), same SL/TP mults
  * breakeven + ATR trailing, opposite-consensus close, dead-market gate
  * next-bar fills; bars are BID quotes (buys pay spread, sells exit at ask)
  * SL checked before TP inside a bar (conservative ambiguity rule)

Timeline (strict, no lookahead):
  bar i closed -> (1) exits vs high/low   [SL/TP as of end of bar i-1]
                  (2) settle winners      (3) consensus on bar i
                  (4) opposite-close @ bar i+1 open
                  (5) dead-market gate + entry @ bar i+1 open
                  (6) BE/trailing updated at bar i's close (effective bar i+1)
                  (7) tuning sample + equity mark

Fast mode (--dump-signals): caches agent votes per bar to state/bt_sig_{sym}.pkl
so backtest.tune can replay price/config variants without re-running agents.

LLM committee + guardian are NOT replayed (non-deterministic / aggregate);
Kronos optional at --kronos-stride N (default off for the fast baseline).
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import quant                                     # noqa: E402
from agents.kronos_agent import forecast_signal              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


# ================================================================== data
def load_bars(symbol: str, days: int, force: bool = False) -> pd.DataFrame:
    """M15 closed bars with a CSV cache (refreshed when older than 10h)."""
    path = os.path.join(STATE, f"bt_{symbol}.csv")
    if os.path.exists(path) and not force:
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h < 10:
            df = pd.read_csv(path, parse_dates=["time"])
            if len(df) >= days * 96 * 0.9:
                return df
    import MetaTrader5 as mt5
    need = max(int(days * 96) + 300, 1000)
    mt5.initialize()
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 1, need)
    mt5.shutdown()
    if rates is None or len(rates) < 700:
        raise SystemExit(f"no history for {symbol}: got "
                         f"{0 if rates is None else len(rates)} bars")
    df = pd.DataFrame(rates)[["time", "open", "high", "low", "close", "spread", "tick_volume"]]
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.sort_values("time").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def symbol_settings(symbol: str) -> dict:
    """Broker symbol properties captured once (mirrors MT5Client.symbol_settings)."""
    cache = os.path.join(STATE, "bt_settings.json")
    alls = json.load(open(cache, encoding="utf-8")) if os.path.exists(cache) else {}
    if symbol in alls:
        return alls[symbol]
    import MetaTrader5 as mt5
    mt5.initialize()
    info = mt5.symbol_info(symbol)
    mt5.shutdown()
    s = {"digits": info.digits, "point": info.point,
         "trade_tick_size": info.trade_tick_size or info.point,
         "trade_tick_value": info.trade_tick_value,
         "trade_contract_size": info.trade_contract_size,
         "volume_min": info.volume_min, "volume_max": info.volume_max,
         "volume_step": info.volume_step,
         "stops_level": info.trade_stops_level, "freezing_level": info.trade_freeze_level}
    alls[symbol] = s
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(alls, fh, indent=1)
    return s


# ================================================================== accounting
def _notional_usd_per_lot(symbol: str, cs: float, price: float) -> float:
    sym = symbol.upper()
    if sym[-3:] == "USD":
        return cs * price
    if sym[:3] == "USD":
        return cs
    return cs * price


def size_volume(symbol: str, info: dict, equity: float, atr_val: float,
                price: float, agents_cfg: dict, risk_cfg: dict) -> float:
    """EXACT live sizing rule (mirrors RiskAgent.size_position)."""
    if float(risk_cfg.get("fixed_lot") or 0) > 0:
        return float(risk_cfg["fixed_lot"])
    sl_distance = max(atr_val * float(agents_cfg["atr_sl_mult"]), 10 * info["point"])
    ticks = sl_distance / info["trade_tick_size"]
    risk_amount = equity * float(risk_cfg["risk_percent_per_trade"]) / 100.0
    loss_per_lot = ticks * info["trade_tick_value"]
    if loss_per_lot <= 0:
        return info["volume_min"]
    volume = risk_amount / loss_per_lot
    lev_cap = float(risk_cfg.get("max_notional_leverage_per_position") or 0)
    if lev_cap > 0 and equity > 0:
        n1 = _notional_usd_per_lot(symbol, info["trade_contract_size"], price)
        if n1 > 0:
            volume = min(volume, equity * lev_cap / n1)
    step = info["volume_step"] or 0.01
    volume = int(volume / step) * step
    return round(max(info["volume_min"], min(info["volume_max"], volume)), 3)


def _settle(symbol: str, entry: float, exitp: float, side: int, vol: float, info: dict) -> float:
    """P/L in USD for each quote convention."""
    sym = symbol.upper()
    diff = (exitp - entry) * side
    cs = info["trade_contract_size"]
    if sym[-3:] == "USD":                      # USD-quoted (EURUSD, XAUUSD)
        return diff * vol * cs
    if sym[:3] == "USD":                       # USD-quoted base (USDJPY): tick-value model
        ticks = abs(exitp - entry) / info["trade_tick_size"]
        return math.copysign(ticks * info["trade_tick_value"], diff or 1.0) * vol
    return diff * vol * cs


def median_atr_ratio(atrs: np.ndarray, i: int, look: int = 96) -> float:
    lo = max(0, i - look)
    med = float(np.nanmedian(atrs[lo:i])) if i > lo else float(atrs[i])
    return float(atrs[i]) / max(med, 1e-12)


# ================================================================== signals cache
def dump_signals(symbols: list[str], days: int) -> None:
    """Run every agent on every bar once; cache votes for fast replay."""
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    a = cfg["agents"]
    for symbol in symbols:
        df = load_bars(symbol, days)
        full = quant.enrich(df.rename(columns={"tick_volume": "volume"}), a)
        names = [ag.name for ag in quant.AGENTS]
        votes = np.zeros((len(full), len(names)), dtype=np.int8)
        confs = np.zeros((len(full), len(names)), dtype=np.float32)
        for ag, k in [(ag, j) for j, ag in enumerate(quant.AGENTS)]:
            for i in range(300, len(full)):
                if not np.isfinite(full["atr"].iloc[i]) or full["atr"].iloc[i] <= 0:
                    continue
                s = ag.analyze(full.iloc[max(0, i - 299): i + 1], a)
                votes[i, k], confs[i, k] = s.direction, s.confidence
        pickle.dump({"df": full, "agents": names, "votes": votes, "confs": confs},
                    open(os.path.join(STATE, f"bt_sig_{symbol}.pkl"), "wb"))
        print(f"cached signals for {symbol}: {len(full)} bars x {len(names)} agents", flush=True)


def load_signals(symbol: str) -> dict:
    path = os.path.join(STATE, f"bt_sig_{symbol}.pkl")
    if not os.path.exists(path):
        raise SystemExit(f"no signal cache for {symbol} — run with --dump-signals first")
    return pickle.load(open(path, "rb"))


# ================================================================== replay core
### shared per-position maintenance used by both simulators
def _apply_manage(pos: dict, cur: float, atr_val: float, spread_px: float,
                  min_dist: float, risk_cfg: dict) -> None:
    """Breakeven lock + ATR trailing (exact live Executor rules)."""
    trail = float(risk_cfg.get("trailing_atr", 1.0)) * atr_val
    be = float(risk_cfg.get("breakeven_atr", 1.0)) * atr_val
    sl = pos["sl"]
    cands = []
    if pos["side"] == 1:
        if cur - pos["entry"] >= be:
            cands.append(pos["entry"] + min_dist + spread_px)
        if cur - trail > (sl or 0):
            cands.append(cur - trail)
        if cands:
            new_sl = max(cands)
            if new_sl < cur - min_dist and new_sl > (sl or 0) + 1e-12:
                pos["sl"] = round(new_sl, 8)
    else:
        if pos["entry"] - cur >= be:
            cands.append(pos["entry"] - min_dist - spread_px)
        if (sl or 0) == 0 or cur + trail < sl:
            cands.append(cur + trail)
        if cands:
            new_sl = min(cands)
            if new_sl > cur + min_dist and new_sl < (sl or 0) - 1e-12:
                pos["sl"] = round(new_sl, 8)


def _check_exits(pos: dict, hi: float, lo: float, spread_px: float) -> bool:
    """Intrabar SL-then-TP ambiguity rule. Returns True if exited."""
    if pos["side"] == 1:
        if lo <= pos["sl"]:
            pos.update(exit_price=pos["sl"], exit_why="sl")
            return True
        if hi >= pos["tp"]:
            pos.update(exit_price=pos["tp"], exit_why="tp")
            return True
    else:
        if hi + spread_px >= pos["sl"]:
            pos.update(exit_price=pos["sl"] + spread_px, exit_why="sl")
            return True
        if lo + spread_px <= pos["tp"]:
            pos.update(exit_price=pos["tp"] + spread_px, exit_why="tp")
            return True
    return False


# ================================================================== simulator (vector helpers + loop)
def replay(full: pd.DataFrame, info: dict, cfg: dict, votes: np.ndarray, confs: np.ndarray,
           names: list[str], weights: dict, threshold: float,
           dead_market_ratio: float = 0.62, start: int = 300,
           end: int | None = None, kronos_signal=None,
           kronos_stride: int = 0, kcfg: dict | None = None) -> dict:
    """Replay decision+execution over [start, end) with given weights/threshold.

    kronos_signal: optional callable(df_slice, atr) -> Signal, refreshed every
    kronos_stride bars when provided (falls back to quant-only otherwise).
    """
    a = cfg["agents"]
    r = cfg["risk"]
    point, digits = info["point"], info["digits"]
    min_dist = max(info["stops_level"], info["freezing_level"]) * point or 2 * point
    cooldown_bars = int(float(r.get("min_gap_seconds_between_same_symbol_trades", 0) or 0) // 60)
    symbol = r.get("_symbol", "EURUSD")

    opens = full["open"].to_numpy()
    highs = full["high"].to_numpy()
    lows = full["low"].to_numpy()
    closes = full["close"].to_numpy()
    spreads = full["spread"].to_numpy() * point
    atrs = full["atr"].to_numpy()
    times = full["time"].astype(str).to_numpy()
    if end is None:
        end = len(full) - 6

    wvec = np.array([float(weights.get(n, 1.0)) for n in names])
    equity = 100_000.0
    trades: list[dict] = []
    opened: list[dict] = []
    curve = np.zeros(end - start, dtype=float)
    samples = []
    last_entry = -10**9
    kg = None                       # cached Kronos (dir, conf)
    kr_bar = -10**9

    for i in range(start, end):
        if not np.isfinite(atrs[i]) or atrs[i] <= 0:
            continue
        sp = float(spreads[i])
        atr_val = float(atrs[i])

        # 1) exits at bar i (sl/tp as of end of bar i-1)
        for pos in opened:
            if pos["exit_bar"] is not None:
                continue
            if _check_exits(pos, highs[i], lows[i], sp):
                pos["exit_bar"] = i
        for pos in opened:
            if pos["exit_bar"] is not None and not pos.get("settled"):
                pos["pnl"] = round(_settle(symbol, pos["entry"], pos["exit_price"],
                                           pos["side"], pos["volume"], info), 2)
                pos["settled"] = True
                equity += pos["pnl"]
                trades.append(pos)
        opened = [p for p in opened if p["exit_bar"] is None]

        # 3) consensus on bar i with the variant's weights/threshold
        dirs_i = votes[i]
        strength = float(np.tanh(np.sum(wvec * confs[i] * dirs_i)))
        tw = float(np.sum(wvec)) or 1.0
        confidence = float(np.clip(np.sum(confs[i] * wvec) / tw, 0.0, 1.0))
        dirs_active = dirs_i[dirs_i != 0]
        if len(np.unique(dirs_active)) == 1 and abs(strength) >= threshold:
            direction = int(dirs_active[0])
        elif abs(strength) >= threshold:
            direction = 1 if strength > 0 else -1
        else:
            direction = 0
        # optional Kronos vote fold-in
        if kronos_signal is not None and kcfg:
            if i - kr_bar >= kronos_stride or kg is None:
                win = full.iloc[max(0, i - 299): i + 1]
                ks = kronos_signal(win, atr_val)
                if ks is not None:
                    kg = (ks.direction, float(ks.confidence))
                    kr_bar = i
            if kg is not None:
                strength = float(np.tanh(strength + kg[0] * kg[1] * float(weights.get("kronos", 1.1))))
                confidence = float(np.clip((confidence * tw + abs(kg[1]) * float(weights.get("kronos", 1.1)))
                                           / (tw + float(weights.get("kronos", 1.1))), 0.0, 1.0))
                if len(np.unique(dirs_active)) == 0 or (len(np.unique(dirs_active)) == 1
                                                        and dirs_active[0] == kg[0]):
                    if abs(strength) >= threshold:
                        direction = kg[0] if direction == 0 else direction
                direction = direction

        # 4) opposite-consensus close
        for pos in opened:
            if direction != 0 and direction != pos["side"] \
                    and abs(strength) >= threshold + 0.10:
                side = pos["side"]
                px = opens[i + 1] - (sp / 2 if side == 1 else -sp / 2)
                pos.update(exit_bar=i, exit_price=px, exit_why="opposite")
                pos["pnl"] = round(_settle(symbol, pos["entry"], px, side, pos["volume"], info), 2)
                pos["settled"] = True
                equity += pos["pnl"]
                trades.append(pos)
        opened = [p for p in opened if p["exit_bar"] is None]

        # 5) dead-market gate + entry
        if not opened and (i - last_entry) >= cooldown_bars and direction != 0:
            if median_atr_ratio(atrs, i) >= dead_market_ratio:
                side = 1 if direction > 0 else -1
                fill = opens[i + 1] + (sp if side == 1 else 0.0)
                sl = round(fill - side * atr_val * float(a["atr_sl_mult"]), digits)
                tp = round(fill + side * atr_val * float(a["atr_tp_mult"]), digits)
                vol = size_volume(symbol, info, equity, atr_val, opens[i + 1], a, r)
                if vol > 0:
                    opened.append({"side": side, "volume": vol, "entry": fill, "entry_bar": i + 1,
                                   "sl": sl, "tp": tp, "atr": round(atr_val, 8),
                                   "strength": round(strength, 3), "conf": round(confidence, 3),
                                   "votes": {n: int(d) for n, d in zip(names, dirs_i)},
                                   "exit_bar": None, "exit_price": None,
                                   "exit_why": "", "pnl": None, "time": str(times[i]),
                                   "symbol": symbol})
                    last_entry = i
                    samples.append((symbol, str(times[i]), "consensus", direction,
                                    round(confidence, 3), 0.0, 0))

        # 6) BE/trailing at bar i close (effective from bar i+1)
        for pos in opened:
            if pos["exit_bar"] is not None:
                continue
            bid_i = closes[i]
            ask_i = closes[i] + sp
            cur = bid_i if pos["side"] == 1 else ask_i
            _apply_manage(pos, cur, atr_val, sp, min_dist, r)

        # 7) tuning sample (agent votes vs forward move, 4 bars in ATR)
        if i + 5 < len(closes) and np.isfinite(atrs[i]) and atrs[i] > 0:
            fwd = (closes[i + 5] - closes[i + 1]) / atr_val
            for k, n in enumerate(names):
                d = int(dirs_i[k])
                samples.append((symbol, str(times[i]), n, d,
                                round(float(confs[i, k]) * float(wvec[k]), 3),
                                round(float(fwd), 3),
                                1 if fwd > 0.2 else (-1 if fwd < -0.2 else 0)))
        curve[i - start] = equity

    # leftovers
    lc = closes[end - 1]
    lsp = spreads[end - 1]
    for pos in opened:
        if pos["exit_bar"] is None:
            px = lc if pos["side"] == 1 else lc + lsp
            pos.update(exit_bar=end - 1, exit_price=px, exit_why="eod")
            pos["pnl"] = round(_settle(symbol, pos["entry"], px, pos["side"], pos["volume"], info), 2)
            pos["settled"] = True
            equity += pos["pnl"]
            trades.append(pos)
    curve[-1] = equity

    return {"trades": trades, "samples": samples, "equity": equity,
            "curve": curve.tolist(), "bars": end - start}


# ================================================================== metrics
def metrics(trades: list[dict], curve: list[float]) -> dict:
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    eq = np.array(curve, dtype=float)
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
    sharpe = float(rets.mean() / rets.std() * math.sqrt(96 * 252)) if rets.std() > 0 else 0.0
    gw, gl = sum(wins), sum(abs(x) for x in losses)
    return {"trades": len(trades),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            "pnl": round(sum(pnls), 2),
            "profit_factor": round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0),
            "avg_win": round(float(np.mean(wins)), 2) if wins else 0.0,
            "avg_loss": round(float(np.mean(losses)), 2) if losses else 0.0,
            "max_drawdown": round(dd, 2),
            "max_drawdown_pct": round(dd / max(float(eq.max()), 1e-9) * 100, 2),
            "sharpe_annualized": round(sharpe, 2)}


def agent_scores(samples: list[tuple]) -> dict:
    if not samples:
        return {}
    df = pd.DataFrame(samples, columns=["symbol", "time", "agent", "dir", "cxw", "fwd", "fwd_dir"])
    df = df[df["dir"] != 0]
    if df.empty:
        return {}
    out = {}
    for agent, g in df.groupby("agent"):
        g = g.copy()
        g["align"] = g["dir"] * g["fwd"]
        acc = float((g["dir"] == g["fwd_dir"]).mean())
        agg = g.assign(aggw=(g["align"] * g["cxw"]).sum() / g["cxw"].abs().sum()
                       if g["cxw"].abs().sum() > 0 else 0.0)
        out[agent] = {"edge_atr": round(float(g["align"].mean()), 4),
                      "hit_pct": round(acc * 100, 1), "n": int(len(g))}
    return out


# ================================================================== full live-like runner (used for the headline report)
def run_symbol_full(symbol: str, df: pd.DataFrame, info: dict, cfg: dict,
                    kronos_stride: int = 0) -> dict:
    """Parse & simulate with the REAL agent objects (slower, mirrors live 1:1)."""
    a = cfg["agents"]
    kcfg = (cfg.get("kronos") or {}) if kronos_stride > 0 and (cfg.get("kronos") or {}).get("enabled", True) else {}

    def kronos_signal(win, atr_val):
        return forecast_signal(symbol, win, kcfg, atr_val, float(a.get("weight_kronos", 1.1))) if kcfg else None

    votes = np.zeros((len(df), len(quant.AGENTS)), dtype=np.int8)
    confs = np.zeros((len(df), len(quant.AGENTS)), dtype=np.float32)
    names = [ag.name for ag in quant.AGENTS]
    weights = {n: float(a.get(f"weight_{n}", 1.0)) for n in names}
    r = dict(cfg["risk"]); r["_symbol"] = symbol
    for ag, k in [(ag, j) for j, ag in enumerate(quant.AGENTS)]:
        for i in range(300, len(df)):
            if not np.isfinite(df["atr"].iloc[i]) or df["atr"].iloc[i] <= 0:
                continue
            s = ag.analyze(df.iloc[max(0, i - 299): i + 1], a)
            votes[i, k], confs[i, k] = s.direction, s.confidence

    res = replay(df, info, cfg, votes, confs, names, weights,
                 float(a["consensus_threshold"]), kronos_signal=kronos_signal,
                 kronos_stride=kronos_stride, kcfg=kcfg)
    # consume the consensus samples from the base replay (not needed for full run)
    return res


# ================================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="EURUSD,GBPUSD,USDJPY,XAUUSD")
    ap.add_argument("--kronos-stride", type=int, default=0)
    ap.add_argument("--dump-signals", action="store_true",
                    help="cache agent votes for the tuner, then exit")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",")]

    if args.dump_signals:
        dump_signals(symbols, args.days)
        return

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    report = {"generated": time.strftime("%Y-%m-%d %H:%M"), "days": args.days,
              "kronos_stride": args.kronos_stride,
              "note": "quant(+Kronos) core without LLM veto/guardian; SL-before-TP",
              "symbols": {}, "agents": {}}
    all_samples = []
    for symbol in symbols:
        print(f"== {symbol} ==", flush=True)
        bars = load_bars(symbol, args.days)
        full = quant.enrich(bars.rename(columns={"tick_volume": "volume"}), cfg["agents"])
        info = symbol_settings(symbol)
        res = run_symbol_full(symbol, full, info, cfg, kronos_stride=args.kronos_stride)
        m = metrics(res["trades"], res["curve"])
        by_why: dict[str, int] = {}
        for t in res["trades"]:
            by_why[t["exit_why"]] = by_why.get(t["exit_why"], 0) + 1
        report["symbols"][symbol] = {"metrics": m, "equity_end": round(res["equity"], 2),
                                     "exits": by_why, "trades": res["trades"]}
        print("   trades=%d win=%.1f%% pnl=%.2f pf=%.2f maxDD=%.2f exits=%s"
              % (m["trades"], m["win_rate_pct"], m["pnl"], m["profit_factor"],
                 m["max_drawdown"], by_why), flush=True)
        # agent-level scores via the signal cache (cheap)
        cache = load_signals(symbol)
        names = cache["agents"]
        weights = {n: float(cfg["agents"].get(f"weight_{n}", 1.0)) for n in names}
        sub = replay(cache["df"], info, cfg, cache["votes"], cache["confs"], names,
                     weights, float(cfg["agents"]["consensus_threshold"]), end=min(len(cache["df"]) - 6, 5000))
        a_samples = [(symbol, t[1], t[2], t[3], t[4], t[5], t[6]) for t in sub["samples"]
                     if t[2] in names]
        scs = agent_scores(a_samples)
        report["agents"] = {**report["agents"], **{f"{symbol}:{k}": v for k, v in scs.items()}}
    report["aggregate_pnl"] = round(sum(v["metrics"]["pnl"] for v in report["symbols"].values()), 2)
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "backtest_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, default=str, indent=1)
    print("\naggregate P/L: %.2f USD | report -> state/backtest_report.json"
          % report["aggregate_pnl"])


if __name__ == "__main__":
    main()
