"""Backtest engine + signal cache for the TradingAgents pipeline.

Replays the live pipeline over MT5 M15 history:
  * same agent code path (quant.AGENTS), same consensus math
  * same risk sizing (1% risk, leverage cap), same SL/TP mults
  * live management: 1R partial close, forced breakeven, stepped ATR trailing,
    optional time stop, opposite-consensus close, dead-market gate
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

# A decision made on bar i uses only bars up to i and is filled at i+1's open.
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                     "H1": 3600, "H4": 14400, "D1": 86400}
DEFAULT_BARS_HISTORY = 300
DEFAULT_EQUITY = 100_000.0


def timeframe_seconds(timeframe: str) -> int:
    return TIMEFRAME_SECONDS.get(str(timeframe).upper(), 900)


def bars_per_day(timeframe: str) -> int:
    return max(1, 86400 // timeframe_seconds(timeframe))


def cooldown_bars(min_gap_seconds: float, timeframe: str) -> int:
    """Bars to wait so the live second-based cooldown is never shortened."""
    gap = float(min_gap_seconds or 0.0)
    if gap <= 0:
        return 0
    return max(1, int(math.ceil(gap / timeframe_seconds(timeframe))))


def bars_path(symbol: str, timeframe: str = "M15") -> str:
    suffix = "" if str(timeframe).upper() == "M15" else f"_{str(timeframe).upper()}"
    return os.path.join(STATE, f"bt_{symbol}{suffix}.csv")


def signals_path(symbol: str, timeframe: str = "M15") -> str:
    suffix = "" if str(timeframe).upper() == "M15" else f"_{str(timeframe).upper()}"
    return os.path.join(STATE, f"bt_sig_{symbol}{suffix}.pkl")


# ================================================================== data
def load_bars(symbol: str, days: int, timeframe: str = "M15",
              force: bool = False, offline: bool = False) -> pd.DataFrame:
    """Closed bars with a CSV cache (fresh for 10h unless ``offline``).

    ``offline`` is for reproducible research runs: it replays the cached file
    without contacting the terminal, and fails loudly when the cache is
    missing instead of silently substituting different data.
    """
    tf = str(timeframe).upper()
    if tf not in TIMEFRAME_SECONDS:
        raise SystemExit(f"unsupported timeframe {timeframe!r}")
    path = bars_path(symbol, tf)
    per_day = bars_per_day(tf)
    if os.path.exists(path) and not force:
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if offline or age_h < 10:
            df = pd.read_csv(path, parse_dates=["time"])
            if "spread" not in df.columns:
                df["spread"] = 0.0
            if "tick_volume" not in df.columns:
                df["tick_volume"] = 0.0
            if offline or len(df) >= days * per_day * 0.9:
                return df
    if offline:
        raise SystemExit(f"no cached {tf} bars for {symbol} at {path}")
    import MetaTrader5 as mt5
    need = max(int(days * per_day) + 300, 1000)
    tf_const = getattr(mt5, f"TIMEFRAME_{tf}", None)
    if tf_const is None:
        raise SystemExit(f"MT5 does not expose TIMEFRAME_{tf}")
    mt5.initialize()
    try:
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, need)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) < 700:
        raise SystemExit(f"no history for {symbol} {tf}: got "
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
                price: float, agents_cfg: dict, risk_cfg: dict,
                sl_distance: float | None = None,
                commission_per_lot: float = 0.0) -> float:
    """Live sizing rule: risk the budget at the actual stop, floor the step.

    Mirrors ``RiskAgent.size_position`` after the reliability upgrade:
    the stop distance comes from the caller (the one actually placed), the
    volume is rounded **down**, a fixed lot is capped by the risk budget, and
    a minimum lot that would exceed the budget is rejected (volume 0) instead
    of being clamped up into over-risk.
    """
    risk_amount = max(0.0, float(equity)) * float(
        risk_cfg["risk_percent_per_trade"]) / 100.0
    if risk_amount <= 0:
        return 0.0
    if sl_distance is None:
        min_dist = max(float(info.get("stops_level") or 0.0),
                       float(info.get("freezing_level") or 0.0)) * info["point"]
        sl_distance = max(atr_val * float(agents_cfg["atr_sl_mult"]),
                          min_dist * 2.0)
    if sl_distance <= 0:
        return 0.0
    loss_per_lot = (sl_distance / info["trade_tick_size"]) * info["trade_tick_value"]
    loss_per_lot += max(0.0, float(commission_per_lot or 0.0))
    if loss_per_lot <= 0:
        return 0.0
    max_by_risk = risk_amount / loss_per_lot
    fixed = float(risk_cfg.get("fixed_lot") or 0.0)
    volume = min(fixed, max_by_risk) if fixed > 0 else max_by_risk
    lev_cap = float(risk_cfg.get("max_notional_leverage_per_position") or 0)
    if lev_cap > 0 and equity > 0:
        n1 = _notional_usd_per_lot(symbol, info["trade_contract_size"], price)
        if n1 > 0:
            volume = min(volume, equity * lev_cap / n1)
    step = float(info["volume_step"] or 0.01)
    vmin = float(info["volume_min"] or step)
    vmax = float(info["volume_max"] or volume)
    if not math.isfinite(volume) or volume < vmin - 1e-9:
        return 0.0
    volume = math.floor(volume / step + 1e-9) * step
    volume = min(volume, vmax)
    if volume < vmin - 1e-9:
        return 0.0
    return round(volume, 8)


def partial_volume(volume: float, info: dict, pct: float) -> float:
    """Volume for a partial close, floored to the broker step.

    Mirrors ``Executor._partial_volume``: never closes below the minimum lot
    and never closes the whole position, so the trade can keep running.
    """
    step = float(info["volume_step"] or 0.01)
    vmin = float(info["volume_min"] or step)
    raw = float(volume) * float(pct) / 100.0
    vol = math.floor(raw / step + 1e-9) * step
    vol = round(vol, 8)
    if vol < vmin - 1e-9:
        return 0.0
    if float(volume) - vol < vmin - 1e-9:
        return 0.0
    return vol


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
def dump_signals(symbols: list[str], days: int, timeframe: str = "M15",
                 offline: bool = False) -> None:
    """Run every agent on every bar once; cache votes for fast replay."""
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    a = cfg["agents"]
    for symbol in symbols:
        df = load_bars(symbol, days, timeframe=timeframe, offline=offline)
        full = quant.enrich(df.rename(columns={"tick_volume": "volume"}), a)
        names = [ag.name for ag in quant.AGENTS]
        votes = np.zeros((len(full), len(names)), dtype=np.int8)
        confs = np.zeros((len(full), len(names)), dtype=np.float32)
        for ag, k in [(ag, j) for j, ag in enumerate(quant.AGENTS)]:
            for i in range(DEFAULT_BARS_HISTORY, len(full)):
                if not np.isfinite(full["atr"].iloc[i]) or full["atr"].iloc[i] <= 0:
                    continue
                s = ag.analyze(full.iloc[max(0, i - (DEFAULT_BARS_HISTORY - 1)): i + 1], a)
                votes[i, k], confs[i, k] = s.direction, s.confidence
        pickle.dump({"df": full, "agents": names, "votes": votes, "confs": confs,
                     "timeframe": str(timeframe).upper()},
                    open(signals_path(symbol, timeframe), "wb"))
        print(f"cached signals for {symbol} {timeframe}: "
              f"{len(full)} bars x {len(names)} agents", flush=True)


def load_signals(symbol: str, timeframe: str = "M15") -> dict:
    path = signals_path(symbol, timeframe)
    if not os.path.exists(path):
        raise SystemExit(f"no signal cache for {symbol} {timeframe} — "
                         f"run with --dump-signals first")
    return pickle.load(open(path, "rb"))


# ================================================================== replay core
### shared per-position maintenance used by both simulators
def _apply_manage(pos: dict, cur: float, atr_val: float, spread_px: float,
                  min_dist: float, risk_cfg: dict,
                  forced_be: bool = False) -> None:
    """Breakeven lock + ATR trailing with the live step threshold.

    Mirrors ``Executor.manage``: the stop only moves when the improvement
    clears ``trailing_step_atr * ATR`` (floored at the broker minimum
    distance), and a partial close forces the breakeven lock even when the
    normal breakeven trigger has not been reached.
    """
    trail = float(risk_cfg.get("trailing_atr", 1.0)) * atr_val
    be = float(risk_cfg.get("breakeven_atr", 1.0)) * atr_val
    step = max(float(risk_cfg.get("trailing_step_atr", 0.0) or 0.0) * atr_val,
               min_dist)
    sl = float(pos.get("sl") or 0.0)
    candidates: list[float] = []
    if pos["side"] == 1:
        if cur - pos["entry"] >= be or forced_be:
            candidates.append(pos["entry"] + min_dist + spread_px)
        trailing = cur - trail
        if sl == 0 or trailing > sl:
            candidates.append(trailing)
        if not candidates:
            return
        levels = ([sl] if sl > 0 else []) + candidates
        new_sl = max(levels)
        if sl > 0 and new_sl - sl < step - 1e-9:
            return
        if new_sl > 0 and new_sl < cur - min_dist:
            pos["sl"] = round(new_sl, 8)
    else:
        if pos["entry"] - cur >= be or forced_be:
            candidates.append(pos["entry"] - min_dist - spread_px)
        trailing = cur + trail
        if sl == 0 or trailing < sl:
            candidates.append(trailing)
        if not candidates:
            return
        levels = ([sl] if sl > 0 else []) + candidates
        new_sl = min(levels)
        if sl > 0 and sl - new_sl < step - 1e-9:
            return
        if new_sl > 0 and new_sl > cur + min_dist:
            pos["sl"] = round(new_sl, 8)


def _check_exits(pos: dict, open_: float, hi: float, lo: float,
                 spread_px: float, slippage_px: float = 0.0) -> bool:
    """Intrabar SL-then-TP rule with gap fills. Returns True if exited.

    Bars are BID quotes.  A long exits at the bid; a short buys back at the
    ask (bid + spread).  When a bar opens beyond a stop/target, the fill is
    the gap open (worse than the order level), never the untouched level.
    Slippage is applied against the trade on every exit.
    """
    if pos["side"] == 1:
        if open_ <= pos["sl"]:
            pos.update(exit_price=open_ - slippage_px, exit_why="sl_gap")
            return True
        if lo <= pos["sl"]:
            pos.update(exit_price=pos["sl"] - slippage_px, exit_why="sl")
            return True
        if open_ >= pos["tp"]:
            pos.update(exit_price=open_ - slippage_px, exit_why="tp_gap")
            return True
        if hi >= pos["tp"]:
            pos.update(exit_price=pos["tp"] - slippage_px, exit_why="tp")
            return True
    else:
        ask_open = open_ + spread_px
        if ask_open >= pos["sl"]:
            pos.update(exit_price=ask_open + slippage_px, exit_why="sl_gap")
            return True
        if hi + spread_px >= pos["sl"]:
            pos.update(exit_price=pos["sl"] + spread_px + slippage_px,
                       exit_why="sl")
            return True
        if ask_open <= pos["tp"]:
            pos.update(exit_price=ask_open + slippage_px, exit_why="tp_gap")
            return True
        if lo + spread_px <= pos["tp"]:
            pos.update(exit_price=pos["tp"] + spread_px + slippage_px,
                       exit_why="tp")
            return True
    return False


# ================================================================== simulator (vector helpers + loop)
def replay(full: pd.DataFrame, info: dict, cfg: dict, votes: np.ndarray,
           confs: np.ndarray, names: list[str], weights: dict,
           threshold: float, dead_market_ratio: float = 0.62,
           start: int = DEFAULT_BARS_HISTORY, end: int | None = None,
           kronos_signal=None, kronos_stride: int = 0, kcfg: dict | None = None,
           symbol: str = "EURUSD", timeframe: str = "M15",
           commission_per_lot: float | None = None,
           slippage_points: float | None = None,
           spread_floor_points: float | None = None,
           bars_history: int = DEFAULT_BARS_HISTORY,
           start_equity: float | None = None) -> dict:
    """Replay decision+execution over [start, end) with given weights/threshold.

    Execution is causal and conservative:
      * a decision made on bar i fills at the open of bar i+1;
      * buys pay the ask (bid + spread), sells exit at the ask;
      * a gap through the stop fills at the gap open, not the stop level;
      * SL is assumed before TP when both are touched in one bar;
      * commission is charged per round turn and slippage against the trade;
      * the live 1R partial close, forced breakeven, stepped trailing and
        optional time stop are evaluated at the bar close;
      * ``spread_floor_points`` is applied when the CSV's spread is zero.

    ``kronos_signal``: optional callable(df_slice, atr) -> Signal, refreshed
    every ``kronos_stride`` bars (falls back to quant-only otherwise).
    """
    a = cfg["agents"]
    r = cfg["risk"]
    bt_cfg = cfg.get("backtest") or {}
    symbol = str(symbol or info.get("symbol") or "EURUSD").upper()
    timeframe = str(timeframe or "M15").upper()
    point, digits = info["point"], info["digits"]
    min_dist = max(float(info.get("stops_level") or 0.0),
                   float(info.get("freezing_level") or 0.0)) * point or 2 * point
    bars_history = max(1, int(bars_history))

    if commission_per_lot is None:
        commission_per_lot = bt_cfg.get(
            "commission_per_lot_round_turn",
            r.get("commission_per_lot_round_turn", 0.0))
    if slippage_points is None:
        slippage_points = bt_cfg.get("slippage_points", 0.0)
    if spread_floor_points is None:
        floors = bt_cfg.get("spread_points_by_symbol") or {}
        spread_floor_points = floors.get(symbol,
                                         bt_cfg.get("spread_points", 0.0))
    if start_equity is None:
        start_equity = bt_cfg.get("start_equity", DEFAULT_EQUITY)
    commission = max(0.0, float(commission_per_lot or 0.0))
    slip_px = max(0.0, float(slippage_points or 0.0)) * point
    cooldown = cooldown_bars(
        r.get("min_gap_seconds_between_same_symbol_trades", 0), timeframe)
    partial_on = bool(r.get("partial_close_enabled", True))
    partial_r = float(r.get("partial_close_at_r", 1.0) or 1.0)
    partial_pct = float(r.get("partial_close_pct", 50.0) or 50.0)
    time_stop_min = float(r.get("time_stop_minutes", 0.0) or 0.0)
    tf_seconds = timeframe_seconds(timeframe)

    opens = full["open"].to_numpy(dtype=float)
    highs = full["high"].to_numpy(dtype=float)
    lows = full["low"].to_numpy(dtype=float)
    closes = full["close"].to_numpy(dtype=float)
    recorded = (full["spread"].to_numpy(dtype=float)
                if "spread" in full.columns else np.zeros(len(full)))
    spreads = np.maximum(recorded, float(spread_floor_points or 0.0)) * point
    atrs = full["atr"].to_numpy(dtype=float)
    times = full["time"].astype(str).to_numpy()
    if end is None:
        end = len(full) - 6
    end = max(start, min(int(end), len(full)))

    wvec = np.array([float(weights.get(n, 1.0)) for n in names])
    equity = float(start_equity)
    trades: list[dict] = []
    opened: list[dict] = []
    curve = np.full(max(0, end - start), equity, dtype=float)
    samples = []
    last_entry = -10**9
    kg = None                       # cached Kronos (dir, conf)
    kr_bar = -10**9

    def finalize(pos: dict, px: float, bar: int, why: str) -> float:
        """Settle the remaining volume; return the net added to equity.

        A partial close is realised earlier, so the trade's reported P/L is
        the sum of the banked partial and the remaining leg, while equity is
        credited leg by leg.
        """
        pos.update(exit_bar=int(bar), exit_price=float(px), exit_why=why,
                   exit_time=str(times[bar]))
        open_volume = float(pos.get("open_volume", pos["volume"]))
        remaining_gross = _settle(symbol, pos["entry"], px, pos["side"],
                                  open_volume, info)
        remaining_net = remaining_gross - open_volume * commission
        total_gross = float(pos.get("realized_gross") or 0.0) + remaining_gross
        total_cost = float(pos["volume"]) * commission
        total_net = float(pos.get("realized_pnl") or 0.0) + remaining_net
        risk_usd = float(pos.get("risk_usd") or 0.0)
        pos.update(pnl_gross=round(total_gross, 2), commission=round(total_cost, 2),
                   pnl=round(total_net, 2), open_volume=round(open_volume, 8),
                   settled=True,
                   bars_held=max(0, int(bar) - int(pos.get("entry_bar", bar))),
                   r_multiple=(round(total_net / risk_usd, 3)
                               if risk_usd > 0 else None))
        return remaining_net

    for i in range(start, end):
        atr_val = float(atrs[i]) if np.isfinite(atrs[i]) else 0.0
        if atr_val <= 0:
            curve[i - start] = equity
            continue
        sp = float(spreads[i])

        # 1) exits at bar i (SL/TP as of end of bar i-1); gaps fill at open
        for pos in opened:
            if _check_exits(pos, opens[i], highs[i], lows[i], sp, slip_px):
                equity += finalize(pos, pos["exit_price"], i, pos["exit_why"])
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
                win = full.iloc[max(0, i - (bars_history - 1)): i + 1]
                ks = kronos_signal(win, atr_val)
                if ks is not None:
                    kg = (ks.direction, float(ks.confidence))
                    kr_bar = i
            if kg is not None:
                strength = float(np.tanh(strength + kg[0] * kg[1]
                                         * float(weights.get("kronos", 1.1))))
                confidence = float(np.clip(
                    (confidence * tw + abs(kg[1]) * float(weights.get("kronos", 1.1)))
                    / (tw + float(weights.get("kronos", 1.1))), 0.0, 1.0))
                if len(np.unique(dirs_active)) == 0 or (len(np.unique(dirs_active)) == 1
                                                        and dirs_active[0] == kg[0]):
                    if abs(strength) >= threshold:
                        direction = kg[0] if direction == 0 else direction

        # 4) opposite-consensus close at the next open (never the same close)
        if direction != 0 and abs(strength) >= threshold + 0.10:
            for pos in opened:
                if direction == pos["side"]:
                    continue
                side = pos["side"]
                nxt = i + 1
                if nxt >= len(opens):
                    continue
                px = (opens[nxt] - slip_px if side == 1
                      else opens[nxt] + float(spreads[nxt]) + slip_px)
                equity += finalize(pos, px, nxt, "opposite")
                trades.append(pos)
            opened = [p for p in opened if p["exit_bar"] is None]

        # 5) dead-market gate + entry at bar i+1's open
        if (not opened and (i - last_entry) >= cooldown and direction != 0
                and i + 1 < end):
            if median_atr_ratio(atrs, i) >= dead_market_ratio:
                side = 1 if direction > 0 else -1
                sp_next = float(spreads[i + 1])
                fill = (opens[i + 1] + sp_next + slip_px if side == 1
                        else opens[i + 1] - slip_px)
                stop_dist = max(atr_val * float(a["atr_sl_mult"]), min_dist * 2.0)
                tp_dist = max(atr_val * float(a["atr_tp_mult"]), min_dist * 1.5)
                sl = round(fill - side * stop_dist, digits)
                tp = round(fill + side * tp_dist, digits)
                vol = size_volume(symbol, info, equity, atr_val, opens[i + 1],
                                  a, r, sl_distance=abs(fill - sl),
                                  commission_per_lot=commission)
                if vol > 0 and fill > 0:
                    risk_usd = (abs(_settle(symbol, fill, sl, side, vol, info))
                                + vol * commission)
                    opened.append({"side": side, "volume": vol, "entry": fill,
                                   "entry_bar": i + 1,
                                   "entry_time": str(times[i + 1]),
                                   "sl": sl, "tp": tp,
                                   "atr": round(atr_val, 8),
                                   "risk_usd": round(risk_usd, 2),
                                   "open_volume": vol,
                                   "risk_dist": abs(fill - sl),
                                   "partial_done": False,
                                   "partials": [],
                                   "realized_pnl": 0.0,
                                   "realized_gross": 0.0,
                                   "strength": round(strength, 3),
                                   "conf": round(confidence, 3),
                                   "votes": {n: int(d) for n, d in zip(names, dirs_i)},
                                   "exit_bar": None, "exit_price": None,
                                   "exit_why": "", "pnl": None,
                                   "time": str(times[i]),
                                   "symbol": symbol})
                    last_entry = i
                    samples.append((symbol, str(times[i]), "consensus", direction,
                                    round(confidence, 3), 0.0, 0))

        # 6) partial profit, time stop, BE/trailing at bar i close
        for pos in opened:
            if int(pos["entry_bar"]) > i:
                continue        # filled at i+1's open; bar i's close predates it
            bid_i = closes[i]
            ask_i = closes[i] + sp
            cur = bid_i if pos["side"] == 1 else ask_i
            if (time_stop_min > 0
                    and (i - int(pos["entry_bar"]) + 1) * tf_seconds
                    >= time_stop_min * 60):
                px = (bid_i - slip_px if pos["side"] == 1
                      else ask_i + slip_px)
                equity += finalize(pos, px, i, "time_stop")
                trades.append(pos)
                continue
            forced_be = False
            if (partial_on and not pos.get("partial_done")
                    and float(pos.get("risk_dist") or 0.0) > 0):
                profit_dist = (cur - pos["entry"] if pos["side"] == 1
                               else pos["entry"] - cur)
                if profit_dist >= partial_r * float(pos["risk_dist"]):
                    vol = partial_volume(pos["open_volume"], info, partial_pct)
                    if vol > 0:
                        px = (bid_i - slip_px if pos["side"] == 1
                              else ask_i + slip_px)
                        gross = _settle(symbol, pos["entry"], px, pos["side"],
                                        vol, info)
                        cost = vol * commission
                        net = gross - cost
                        pos["partials"].append({
                            "bar": int(i), "time": str(times[i]),
                            "volume": round(vol, 8), "price": round(px, 8),
                            "pnl_gross": round(gross, 2),
                            "commission": round(cost, 2),
                            "pnl": round(net, 2)})
                        pos["open_volume"] = round(
                            float(pos["open_volume"]) - vol, 8)
                        pos["partial_done"] = True
                        pos["realized_pnl"] = (
                            float(pos.get("realized_pnl") or 0.0) + net)
                        pos["realized_gross"] = (
                            float(pos.get("realized_gross") or 0.0) + gross)
                        equity += net
                        forced_be = True
            _apply_manage(pos, cur, atr_val, sp, min_dist, r,
                          forced_be=forced_be)
        opened = [p for p in opened if p["exit_bar"] is None]

        # 7) tuning sample (agent votes vs forward move, 4 bars in ATR)
        if i + 5 < len(closes):
            fwd = (closes[i + 5] - closes[i + 1]) / atr_val
            for k, n in enumerate(names):
                d = int(dirs_i[k])
                samples.append((symbol, str(times[i]), n, d,
                                round(float(confs[i, k]) * float(wvec[k]), 3),
                                round(float(fwd), 3),
                                1 if fwd > 0.2 else (-1 if fwd < -0.2 else 0)))
        curve[i - start] = equity

    # leftovers are closed at the last close of the simulated window
    if end > start:
        last = end - 1
        for pos in opened:
            px = (closes[last] - slip_px if pos["side"] == 1
                  else closes[last] + float(spreads[last]) + slip_px)
            equity += finalize(pos, px, last, "eod")
            trades.append(pos)
        opened = []
        curve[-1] = equity

    return {"trades": trades, "samples": samples, "equity": equity,
            "curve": curve.tolist(), "bars": end - start,
            "symbol": symbol, "timeframe": timeframe,
            "costs": {"commission_per_lot": commission,
                      "slippage_points": float(slippage_points or 0.0),
                      "spread_floor_points": float(spread_floor_points or 0.0)},
            "start_equity": float(start_equity)}


# ================================================================== metrics
def _streaks(trades: list[dict]) -> tuple[int, int]:
    """Longest winning and losing runs, in exit order."""
    best_win = best_loss = cur_win = cur_loss = 0
    for trade in sorted(trades, key=lambda t: int(t.get("exit_bar") or 0)):
        pnl = trade.get("pnl")
        if pnl is None:
            continue
        if pnl > 0:
            cur_win, cur_loss = cur_win + 1, 0
            best_win = max(best_win, cur_win)
        else:
            cur_loss, cur_win = cur_loss + 1, 0
            best_loss = max(best_loss, cur_loss)
    return best_win, best_loss


def _side_block(trades: list[dict], side: int) -> dict:
    rows = [t for t in trades
            if int(t.get("side") or 0) == side and t.get("pnl") is not None]
    pnls = [float(t["pnl"]) for t in rows]
    rs = [float(t["r_multiple"]) for t in rows
          if t.get("r_multiple") is not None]
    return {
        "trades": len(rows),
        "wins": sum(1 for p in pnls if p > 0),
        "win_rate_pct": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1)
        if pnls else 0.0,
        "pnl": round(sum(pnls), 2),
        "avg_r": round(float(np.mean(rs)), 3) if rs else None,
    }


def _monthly(trades: list[dict]) -> dict:
    months: dict[str, float] = {}
    for trade in trades:
        pnl = trade.get("pnl")
        stamp = str(trade.get("exit_time") or "")
        if pnl is None or len(stamp) < 7:
            continue
        month = stamp[:7]
        months[month] = round(months.get(month, 0.0) + float(pnl), 2)
    return dict(sorted(months.items()))


def metrics(trades: list[dict], curve: list[float], timeframe: str = "M15",
            start_equity: float | None = None) -> dict:
    """Performance summary of a replay.

    Every figure is derived from the simulated trades/curve.  ``sharpe`` is
    annualised with the number of bars per year for the replayed timeframe,
    not a hard-coded M15 constant.
    """
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [float(t["r_multiple"]) for t in trades
          if t.get("r_multiple") is not None]
    gross = [float(t.get("pnl_gross") or 0.0) for t in trades
             if t.get("pnl") is not None]
    commissions = sum(float(t.get("commission") or 0.0) for t in trades
                      if t.get("pnl") is not None)
    eq = np.array(curve, dtype=float)
    if len(eq):
        peaks = np.maximum.accumulate(eq)
        dd = float(np.max(peaks - eq))
        start = float(start_equity if start_equity is not None else eq[0])
        dd_pct = dd / max(float(peaks.max()), 1e-9) * 100
        rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
        bars_yr = bars_per_day(timeframe) * 252
        sharpe = (float(rets.mean() / rets.std() * math.sqrt(bars_yr))
                  if len(rets) and rets.std() > 0 else 0.0)
        final_equity = float(eq[-1])
    else:
        dd = dd_pct = sharpe = 0.0
        start = float(start_equity or DEFAULT_EQUITY)
        final_equity = start
    gw, gl = sum(wins), sum(abs(x) for x in losses)
    best_win, best_loss = _streaks(trades)
    avg_bars = (float(np.mean([t["bars_held"] for t in trades
                               if t.get("bars_held") is not None]))
                if any(t.get("bars_held") is not None for t in trades) else 0.0)
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "pnl": round(sum(pnls), 2),
        "gross_pnl": round(sum(gross), 2),
        "commission_paid": round(commissions, 2),
        "profit_factor": round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0),
        "expectancy_usd": round(float(np.mean(pnls)), 2) if pnls else 0.0,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else None,
        "avg_r": round(float(np.mean(rs)), 3) if rs else None,
        "avg_win": round(float(np.mean(wins)), 2) if wins else 0.0,
        "avg_loss": round(float(np.mean(losses)), 2) if losses else 0.0,
        "max_win_streak": best_win,
        "max_loss_streak": best_loss,
        "avg_bars_held": round(avg_bars, 1),
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": round(dd_pct, 2),
        "start_equity": round(start, 2),
        "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity - start) / max(start, 1e-9) * 100, 2),
        "sharpe_annualized": round(sharpe, 2),
        "bars_per_year": int(bars_per_day(timeframe) * 252),
        "timeframe": str(timeframe).upper(),
        "long": _side_block(trades, 1),
        "short": _side_block(trades, -1),
        "monthly": _monthly(trades),
    }


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
                    kronos_stride: int = 0, timeframe: str = "M15",
                    commission_per_lot: float | None = None,
                    slippage_points: float | None = None,
                    spread_floor_points: float | None = None,
                    bars_history: int = DEFAULT_BARS_HISTORY,
                    start_equity: float | None = None) -> dict:
    """Parse & simulate with the REAL agent objects (slower, mirrors live 1:1).

    The window handed to each agent is the live ``bars_history`` window ending
    at the closed bar, so no agent can see a future candle.  All execution
    accounting is delegated to :func:`replay`, with the symbol and timeframe
    passed explicitly (the old ``risk._symbol`` hack is gone).
    """
    a = cfg["agents"]
    kcfg = (cfg.get("kronos") or {}) if (
        kronos_stride > 0 and (cfg.get("kronos") or {}).get("enabled", True)) else {}

    def kronos_signal(win, atr_val):
        return forecast_signal(symbol, win, kcfg, atr_val,
                               float(a.get("weight_kronos", 1.1))) if kcfg else None

    bars_history = max(1, int(bars_history))
    votes = np.zeros((len(df), len(quant.AGENTS)), dtype=np.int8)
    confs = np.zeros((len(df), len(quant.AGENTS)), dtype=np.float32)
    names = [ag.name for ag in quant.AGENTS]
    weights = {n: float(a.get(f"weight_{n}", 1.0)) for n in names}
    for ag, k in [(ag, j) for j, ag in enumerate(quant.AGENTS)]:
        for i in range(bars_history, len(df)):
            if not np.isfinite(df["atr"].iloc[i]) or df["atr"].iloc[i] <= 0:
                continue
            window = df.iloc[max(0, i - (bars_history - 1)): i + 1]
            s = ag.analyze(window, a)
            votes[i, k], confs[i, k] = s.direction, s.confidence

    return replay(df, info, cfg, votes, confs, names, weights,
                  float(a["consensus_threshold"]),
                  dead_market_ratio=float(
                      cfg["risk"].get("dead_market_atr_ratio", 0.62)),
                  kronos_signal=kronos_signal,
                  kronos_stride=kronos_stride, kcfg=kcfg,
                  start=bars_history, symbol=symbol, timeframe=timeframe,
                  commission_per_lot=commission_per_lot,
                  slippage_points=slippage_points,
                  spread_floor_points=spread_floor_points,
                  bars_history=bars_history, start_equity=start_equity)


# ================================================================== main
def _cost_report(res: dict) -> dict:
    return {"commission_per_lot": res["costs"]["commission_per_lot"],
            "slippage_points": res["costs"]["slippage_points"],
            "spread_floor_points": res["costs"]["spread_floor_points"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="EURUSD,GBPUSD,USDJPY,XAUUSD")
    ap.add_argument("--timeframe", type=str, default=None,
                    help="bar timeframe (default: trading.timeframe from config)")
    ap.add_argument("--kronos-stride", type=int, default=0)
    ap.add_argument("--offline", action="store_true",
                    help="replay the cached CSV only; never contact MT5")
    ap.add_argument("--spread-points", type=float, default=None,
                    help="minimum spread charged, in points")
    ap.add_argument("--commission", type=float, default=None,
                    help="round-turn commission per lot, account currency")
    ap.add_argument("--slippage-points", type=float, default=None,
                    help="adverse slippage per fill, in points")
    ap.add_argument("--start-equity", type=float, default=None)
    ap.add_argument("--bars-history", type=int, default=DEFAULT_BARS_HISTORY)
    ap.add_argument("--out", type=str, default=os.path.join(STATE, "backtest_report.json"))
    ap.add_argument("--dump-signals", action="store_true",
                    help="cache agent votes for the tuner, then exit")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    timeframe = str(args.timeframe or cfg["trading"].get("timeframe") or "M15").upper()
    if timeframe not in TIMEFRAME_SECONDS:
        raise SystemExit(f"unsupported timeframe {timeframe!r}")

    if args.dump_signals:
        dump_signals(symbols, args.days, timeframe=timeframe,
                     offline=args.offline)
        return

    report = {"generated": time.strftime("%Y-%m-%d %H:%M"),
              "days": args.days, "timeframe": timeframe,
              "offline": bool(args.offline),
              "kronos_stride": args.kronos_stride,
              "bars_history": args.bars_history,
              "note": ("quant(+Kronos) core without LLM veto/guardian; next-bar "
                       "fills, bid/ask spread, gap fills, SL-before-TP, "
                       "round-turn commission and slippage"),
              "cost_overrides": {
                  "spread_points": args.spread_points,
                  "commission_per_lot_round_turn": args.commission,
                  "slippage_points": args.slippage_points,
                  "start_equity": args.start_equity,
              },
              "symbols": {}, "agents": {}}
    for symbol in symbols:
        print(f"== {symbol} {timeframe} ==", flush=True)
        bars = load_bars(symbol, args.days, timeframe=timeframe,
                         offline=args.offline)
        min_bars = max(700, int(args.bars_history) + 100)
        if len(bars) < min_bars:
            raise SystemExit(
                f"not enough {timeframe} bars for {symbol}: {len(bars)} < "
                f"{min_bars}; increase --days or refresh the cache")
        full = quant.enrich(bars.rename(columns={"tick_volume": "volume"}),
                            cfg["agents"])
        info = symbol_settings(symbol)
        res = run_symbol_full(
            symbol, full, info, cfg, kronos_stride=args.kronos_stride,
            timeframe=timeframe, commission_per_lot=args.commission,
            slippage_points=args.slippage_points,
            spread_floor_points=args.spread_points,
            bars_history=args.bars_history, start_equity=args.start_equity)
        m = metrics(res["trades"], res["curve"], timeframe=timeframe,
                    start_equity=res.get("start_equity"))
        by_why: dict[str, int] = {}
        for trade in res["trades"]:
            by_why[trade["exit_why"]] = by_why.get(trade["exit_why"], 0) + 1
        report["symbols"][symbol] = {
            "metrics": m, "equity_end": round(res["equity"], 2),
            "exits": by_why, "costs": _cost_report(res),
            "data": {"bars": int(len(bars)),
                     "first": str(bars["time"].iloc[0]),
                     "last": str(bars["time"].iloc[-1]),
                     "path": bars_path(symbol, timeframe)},
            "trades": res["trades"]}
        print("   trades=%d win=%.1f%% pnl=%.2f pf=%.2f maxDD=%.2f "
              "expectancy=%.2f exits=%s"
              % (m["trades"], m["win_rate_pct"], m["pnl"],
                 m["profit_factor"], m["max_drawdown"],
                 m["expectancy_usd"], by_why), flush=True)
        # agent-level scores from the same causal samples the replay produced
        samples = [(symbol, t[1], t[2], t[3], t[4], t[5], t[6])
                   for t in res["samples"] if t[2] in names]
        scores = agent_scores(samples)
        report["agents"] = {**report["agents"],
                            **{f"{symbol}:{k}": v for k, v in scores.items()}}
    report["aggregate_pnl"] = round(
        sum(v["metrics"]["pnl"] for v in report["symbols"].values()), 2)
    report["aggregate_trades"] = sum(
        v["metrics"]["trades"] for v in report["symbols"].values())
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, default=str, indent=1)
    print("\naggregate P/L: %.2f USD over %d trades | report -> %s"
          % (report["aggregate_pnl"], report["aggregate_trades"], args.out))


if __name__ == "__main__":
    main()
