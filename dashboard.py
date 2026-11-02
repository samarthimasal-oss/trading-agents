"""
TradingAgents Dashboard
=======================
Professional local web dashboard (http://localhost:5005):
  - MT5 account snapshot (equity / balance / P&L / margin)
  - Live signal board per symbol (consensus, agents votes, AI verdict)
  - Candlestick chart with EMA overlays, pulled straight from MT5
  - Strategy panel (the agent desk), decisions journal, live log tail
  - Start / Stop controls for the runner (dry-run or live), guardian reset

Run:
    python dashboard.py            # opens browser automatically
    python dashboard.py --no-open
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import webbrowser

from flask import Flask, Response, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.state_store import StateStore          # noqa: E402
from core.quotes import assess_quote, spread_limit_for             # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.json")
PID_FILE = os.path.join(ROOT, "state", "dashboard.json")
LIVE_FILE = os.path.join(ROOT, "state", "live.json")

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask("tradingagents-dashboard")
_client_cache: dict = {}
_runner_proc: subprocess.Popen | None = None

STRATEGY_INFO = {
    "trend": ("EMA fast/slow crossover with slope quality",
              "rides sustained directional moves"),
    "momentum": ("RSI bands + short-term rate of change",
                 "catches thrusts and exhaustion"),
    "reversion": ("ATR-stretch mean reversion around the slow EMA",
                  "fades overextension"),
    "breakout": ("Donchian range breakout with ATR confirmation",
                 "enters volatility expansions"),
    "pullback": ("major-trend pullback to EMA20",
                 "buys dips / sells rips in trends"),
    "kronos": ("Kronos foundation model (Transformer) forecasts the next 16 candles "
               "from raw OHLCV history", "deep-learning view of the near future"),
}


def load_cfg() -> dict:
    with open(CFG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_client():
    """Reusable MT5 client; retries connect on demand."""
    from core.mt5_client import MT5Client
    cfg = load_cfg()
    c = _client_cache.get("c")
    if c is None:
        c = MT5Client(cfg["mt5"])
        _client_cache["c"] = c
        c.connect()                       # may raise -> run state stays clean
    return c


# --------------------------------------------------------------------- MT5
def get_account_snapshot() -> dict:
    try:
        c = get_client()
        acc = c.account()
        cfg = load_cfg()
        magic = int(cfg["trading"]["magic"])
        poss = c.positions(magic=magic)
        quotes = {}
        for sym in cfg["trading"].get("symbols") or [cfg["trading"]["symbol"]]:
            try:
                resolved = c.resolve_symbol(sym)
                info = c.prepare(resolved)
                t = c.tick(resolved)
                q = assess_quote(t.bid, t.ask, info.point,
                                 max_spread_points=spread_limit_for(sym, cfg["risk"]))
                quotes[sym] = {"resolved": resolved, "bid": t.bid, "ask": t.ask,
                               **q.as_dict()}
            except Exception as exc:              # one bad symbol must not blank the page
                quotes[sym] = {"valid": False, "reason": str(exc)[:80]}
        return {
            "connected": True,
            "login": acc.login,
            "currency": acc.currency,
            "balance": round(acc.balance, 2),
            "equity": round(acc.equity, 2),
            "free_margin": round(acc.free_margin, 2),
            "margin_level": round(acc.margin_level, 0),
            "floating": round(acc.profit, 2),
            "daily_pnl": round(c.daily_bot_profit(magic), 2),
            "quotes": quotes,
            "positions": [
                {"ticket": p.ticket,
                 "dir": "BUY" if p.type == 0 else "SELL",
                 "symbol": p.symbol, "volume": p.volume,
                 "open": p.price_open, "sl": p.sl, "tp": p.tp,
                 "pl": round(p.profit, 2)}
                for p in poss
            ],
        }
    except Exception as exc:
        _client_cache.pop("c", None)
        return {"connected": False, "error": str(exc)[:120]}


# ------------------------------------------------------------------ journal
def journal_snapshot() -> dict:
    cfg = load_cfg()
    store = StateStore(cfg["logging"]["db_file"])
    try:
        rows = store.recent_decisions(14)
        decisions = [
            {"when": time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])),
             "symbol": r["symbol"], "action": r["action"],
             "dir": "+" if r["direction"] > 0 else "-" if r["direction"] < 0 else "o",
             "volume": r["volume"], "strength": round(r["strength"], 2),
             "conf": round(r["confidence"], 2), "llm": r["llm_action"],
             "executed": bool(r["executed"]), "reason": (r["reason"] or "")[:90]}
            for r in rows
        ]
        cur = store.db.execute(
            "SELECT ts, strength FROM decisions ORDER BY ts DESC LIMIT 180")
        strength = [(r[0], r[1]) for r in reversed(cur.fetchall())]
        cur = store.db.execute(
            "SELECT ts, equity FROM equity_log WHERE equity > 0 ORDER BY ts DESC LIMIT 400")
        equity = [(r[0], r[1]) for r in reversed(cur.fetchall())]
        # ---- today's decision funnel (local midnight)
        midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        funnel = {"filled": 0, "vetoed": 0, "blocked": 0, "flat": 0, "other": 0}
        try:
            cur = store.db.execute(
                "SELECT action, executed, COUNT(*) FROM decisions "
                "WHERE ts >= ? GROUP BY action, executed", (midnight,))
            for action, executed, cnt in cur.fetchall():
                if action in ("buy", "sell") and executed:
                    funnel["filled"] += cnt
                elif action == "vetoed":
                    funnel["vetoed"] += cnt
                elif action == "blocked":
                    funnel["blocked"] += cnt
                elif action == "none":
                    funnel["flat"] += cnt
                else:
                    funnel["other"] += cnt
        except Exception:
            pass
        # ---- execution event feed (fills / closes / SL moves / rejects)
        events = []
        try:
            cur = store.db.execute(
                "SELECT ts, kind, symbol, detail FROM events ORDER BY ts DESC LIMIT 14")
            events = [{"ts": r[0], "kind": r[1], "symbol": r[2], "detail": (r[3] or "")[:90]}
                      for r in reversed(cur.fetchall())]
        except Exception:
            pass
        stats = store.stats()
        return {"decisions": decisions, "strength": strength,
                "equity": equity, "stats": stats,
                "funnel": funnel, "events": events, "peak": store.peak_equity()}
    finally:
        store.close()


def live_board() -> dict:
    """The runner's compact signal board (state/live.json)."""
    try:
        with open(LIVE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        data["age"] = round(time.time() - float(data.get("ts", 0)), 0)
        return data
    except Exception:
        return {}


def kronos_score_summary() -> dict:
    """Live forecast accuracy scorecard (graded on every closed bar)."""
    try:
        from core import kronos_score
        return kronos_score.summary()
    except Exception as exc:                          # noqa: BLE001
        log.warning("kronos scorecard unavailable: %s", exc)
        return {}


_PLAN_CACHE: dict = {"ts": 0.0, "data": []}


def trade_plans(mt5_state: dict, live: dict) -> list[dict]:
    """Next-trade plan per symbol: entry / SL / TP / lots / risk / blockers.

    Mirrors the risk agent's sizing maths against the live equity and the
    runner's current consensus, without touching the runner's state.
    """
    now = time.time()
    if now - float(_PLAN_CACHE.get("ts") or 0) < 4 and _PLAN_CACHE.get("data"):
        return _PLAN_CACHE["data"]

    cfg = load_cfg()
    t, a, r = cfg["trading"], cfg["agents"], cfg["risk"]
    equity = float((mt5_state or {}).get("equity") or 0.0)
    positions = (mt5_state or {}).get("positions") or []
    board = ((live or {}).get("symbols") or {})
    symbols = list(board.keys()) or list(t.get("symbols") or [t["symbol"]])
    risk_pct = float(r.get("risk_percent_per_trade", 0.5))
    risk_usd = equity * risk_pct / 100.0
    sl_mult = float(a.get("atr_sl_mult", 1.5))
    tp_mult = float(a.get("atr_tp_mult", 3.0))
    lev_cap = float(r.get("max_notional_leverage_per_position") or 0.0)
    max_positions = int(r.get("max_positions", 2))
    max_spread = float(r.get("max_spread_points", 40))

    def valid_board_quote(p: dict) -> bool:
        """A usable quote needs a positive price and a sane, non-crossed spread."""
        try:
            price = float(p.get("price") or 0.0)
            spread = float(p.get("spread") or 0.0)
        except (TypeError, ValueError):
            return False
        return price > 0 and 0.0 <= spread <= 1000.0

    rows = []
    for sym in symbols:
        b = board.get(sym) or {}
        row = {"symbol": sym, "dir": int(b.get("dir") or 0),
               "strength": b.get("strength"), "atr": b.get("atr"),
               "spread": b.get("spread"), "risk_usd": round(risk_usd, 2),
               "status": "WAITING", "tone": "idle", "detail": "no consensus yet",
               "entry": None, "sl": None, "tp": None, "lots": None,
               "rr": None, "htf": [], "news": b.get("news") or ""}
        try:
            d = b.get("forecasts") or {}
            for tf in ("H1", "H4", "D1"):
                fc = d.get(tf)
                if fc:
                    row["htf"].append({"tf": tf, "dir": int(fc.get("dir") or 0),
                                       "agree": fc.get("agreement")})
        except Exception:                             # noqa: BLE001
            pass

        pos = next((p for p in positions if p.get("symbol") == sym), None)
        if pos:
            pdir = -1 if str(pos.get("dir")).upper() == "SELL" else 1
            entry_p = float(pos.get("open") or 0.0)
            sl_p = float(pos.get("sl") or 0.0)
            tp_p = float(pos.get("tp") or 0.0)
            rr = None
            if entry_p > 0 and sl_p > 0 and tp_p > 0:
                risk_dist = abs(entry_p - sl_p)
                if risk_dist > 0:
                    rr = round(abs(tp_p - entry_p) / risk_dist, 2)
            row.update({"dir": pdir, "status": "IN TRADE", "tone": "live",
                        "detail": f"#{pos.get('ticket')} {pos.get('dir')} "
                                  f"{pos.get('volume')} lots · P/L {pos.get('pl')}",
                        "entry": pos.get("open"), "sl": pos.get("sl"),
                        "tp": pos.get("tp"), "lots": pos.get("volume"),
                        "rr": rr})
            rows.append(row)
            continue

        atr = float(b.get("atr") or 0.0)
        if row["dir"] == 0 or atr <= 0:
            if valid_board_quote(b):
                row["entry"] = b.get("price")          # show where price is now
                row["detail"] = ("waiting for a consensus above "
                                 f"{float(a.get('consensus_threshold', 0.3)):.2f}")
            else:
                row.update({"status": "STALE", "tone": "warn",
                            "detail": "broker quote out of range — waiting for the feed",
                            "spread": None, "entry": None})
            rows.append(row)
            continue

        try:
            from core.mt5_client import MT5Client
            c = get_client()
            info = c.prepare(sym)
            s = MT5Client.symbol_settings(info)
            tick = c.tick(sym)
            if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
                row.update({"status": "STALE", "tone": "warn",
                            "detail": "broker quote out of range — waiting for the feed",
                            "spread": None, "entry": None})
                rows.append(row)
                continue
            mid = (float(tick.bid) + float(tick.ask)) / 2.0
            quote_spread = ((float(tick.ask) - float(tick.bid)) / s["point"]
                            if s.get("point") else 0.0)
            row["spread"] = round(quote_spread, 1)
            stop = max(atr * sl_mult, s["stops_level"] * s["point"] * 2.0)
            tp_dist = atr * tp_mult
            entry = float(tick.ask) if row["dir"] > 0 else float(tick.bid)
            sl = entry - stop if row["dir"] > 0 else entry + stop
            tp = entry + tp_dist if row["dir"] > 0 else entry - tp_dist

            # position size — same maths as RiskAgent.size_position
            ticks = stop / s["trade_tick_size"]
            loss_per_lot = ticks * s["trade_tick_value"]
            vol = (risk_usd / loss_per_lot) if loss_per_lot > 0 else s["volume_min"]
            if lev_cap > 0 and equity > 0:
                cs = float(s.get("trade_contract_size") or 0.0)
                sym_u = sym.upper()
                if sym_u[-3:] == "USD":          # EURUSD/XAUUSD: lots * contract * price
                    notional = cs * mid
                elif sym_u[:3] == "USD":         # USDJPY: $100k per lot regardless of quote
                    notional = cs
                else:
                    notional = cs * mid
                if notional > 0:
                    vol = min(vol, equity * lev_cap / notional)
            step = s["volume_step"] or 0.01
            vol = int(vol / step) * step
            vol = max(s["volume_min"], min(s["volume_max"], vol))
            vol = round(vol, 3)

            spread = float(quote_spread or 0.0)
            if not (0 <= spread <= 1000):
                row.update({"status": "STALE", "tone": "warn",
                            "detail": "broker quote out of range — waiting for the feed",
                            "spread": None, "entry": None})
                rows.append(row)
                continue
            elif spread > max_spread:
                row.update({"status": "BLOCKED", "tone": "warn",
                            "detail": f"spread {spread:.0f} > {max_spread:.0f} pts"})
            elif len(positions) >= max_positions:
                row.update({"status": "BLOCKED", "tone": "warn",
                            "detail": f"max {max_positions} positions open"})
            elif b.get("news"):
                row.update({"status": "NEWS", "tone": "warn",
                            "detail": str(b.get("news"))[:80]})
            elif str(b.get("action") or "").startswith(("blocked", "vetoed")):
                row.update({"status": "BLOCKED", "tone": "warn",
                            "detail": str(b.get("why") or b.get("action"))[:80]})
            elif str(b.get("action") or "") == "order-failed":
                row.update({"status": "BLOCKED", "tone": "warn",
                            "detail": "broker rejected the last order (no quotes) — retry next bar"})
            else:
                row.update({"status": "READY", "tone": "good",
                            "detail": "valid on the next M15 bar close"})

            row.update({
                "entry": round(entry, s["digits"]),
                "sl": round(sl, s["digits"]),
                "tp": round(tp, s["digits"]),
                "lots": vol,
                "rr": round(tp_dist / stop, 2) if stop > 0 else None,
                "price": round(mid, s["digits"]),
            })
        except Exception as exc:                      # noqa: BLE001
            row.update({"status": "NO DATA", "tone": "warn",
                        "detail": str(exc)[:80]})
        rows.append(row)

    _PLAN_CACHE["ts"] = now
    _PLAN_CACHE["data"] = rows
    return rows



def strategies_list() -> list[dict]:
    cfg = load_cfg()["agents"]
    out = []
    for name, weight_key in [("trend", "weight_trend"), ("momentum", "weight_momentum"),
                             ("reversion", "weight_reversion"),
                             ("breakout", "weight_breakout"),
                             ("pullback", "weight_pullback"),
                             ("kronos", "weight_kronos")]:
        how, why = STRATEGY_INFO[name]
        out.append({"name": name, "weight": cfg.get(weight_key, 0.0),
                    "how": how, "why": why})
    return out


def log_tail(n_lines: int = 30) -> str:
    cfg = load_cfg()
    path = cfg["logging"].get("log_file")
    if not path or not os.path.exists(path):
        return "(log file appears once the runner has started)"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-n_lines:])


# ------------------------------------------------------------------ controls
def runner_state() -> dict:
    state = {"mode": "stopped", "pid": None}
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, encoding="utf-8") as fh:
                state.update(json.load(fh))
        except Exception:
            pass
    if state.get("pid"):
        if not _pid_alive(int(state["pid"])):
            state["mode"] = "stopped"
            state["pid"] = None
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
    return state


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10).stdout or ""
        return str(pid) in out
    except Exception:
        return False


def start_runner(mode: str) -> dict:
    global _runner_proc
    if runner_state()["mode"] != "stopped":
        return {"ok": False, "error": "runner already running"}
    cmd = [sys.executable, "-u", os.path.join(ROOT, "main.py"), "run"]
    if mode == "dry":
        cmd.append("--dry-run")
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    _runner_proc = subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=open(os.path.join(ROOT, "state", "run.log"), "ab"),
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": _runner_proc.pid,
                   "mode": "dry-run" if mode == "dry" else "LIVE",
                   "started": time.time()}, fh)
    return {"ok": True, "pid": _runner_proc.pid, "mode": mode}


def stop_runner() -> dict:
    global _runner_proc
    state = runner_state()
    pid = state.get("pid")
    _runner = _runner_proc
    if not pid and _runner:
        pid = _runner.pid
    if not pid:
        return {"ok": False, "error": "nothing to stop"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=15)
        return {"ok": True, "killed": pid}
    finally:
        _runner_proc = None
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


# --------------------------------------------------------------- endpoints
@app.get("/api/snapshot")
def api_snapshot() -> Response:
    cfg = load_cfg()
    mt5_state = get_account_snapshot()
    js = journal_snapshot()
    runner = runner_state()
    live = live_board()
    return jsonify({
        "mt5": mt5_state,
        "runner": runner,
        "trading_cfg": {
            "symbols": cfg["trading"].get("symbols") or [cfg["trading"]["symbol"]],
            "timeframe": cfg["trading"]["timeframe"],
            "risk_pct": cfg["risk"]["risk_percent_per_trade"],
            "daily_loss_limit": cfg["risk"]["max_daily_loss_percent"],
            "dead_ratio": float(cfg["risk"].get("dead_market_atr_ratio", 0.62)),
            "max_dd_limit": float(cfg["risk"].get("max_equity_drawdown_percent", 15.0)),
            "kronos_tfs": ["M15"] + [str(x).upper() for x in (cfg.get("kronos") or {}).get("timeframes") or []],
            "kronos_runs": int((cfg.get("kronos") or {}).get("ensemble_runs", 1) or 1),
            "min_agreement": float((cfg.get("kronos") or {}).get("min_agreement", 0.6)),
            "htf_confluence": float((cfg.get("kronos") or {}).get("htf_confluence", 0.0)),
            "partial_close": bool(cfg["risk"].get("partial_close_enabled", True)),
            "partial_close_at_r": float(cfg["risk"].get("partial_close_at_r", 1.0)),
            "partial_close_pct": float(cfg["risk"].get("partial_close_pct", 50)),
            "time_stop_minutes": float(cfg["risk"].get("time_stop_minutes", 0) or 0),
            "llm_model": cfg["llm"].get("gemini_model"),
            "llm_enabled": cfg["llm"].get("enabled", True),
        },
        "strategies": strategies_list(),
        "live": live,
        "plan": trade_plans(mt5_state, live),
        "kronos_score": kronos_score_summary(),
        **js,
        "log_tail": log_tail(30),
    })


@app.get("/api/chart")
def api_chart() -> Response:
    """OHLC candles + EMAs for the chart panel, straight from MT5.
    Optional ``tf`` argument switches timeframe (M15/H1/H4/D1); the Kronos
    forecast overlay for that timeframe comes from the runner's live board."""
    cfg = load_cfg()
    symbols = cfg["trading"].get("symbols") or [cfg["trading"]["symbol"]]
    symbol = request.args.get("symbol") or symbols[0]
    fallback = next((s for s in symbols if s.lower().startswith(symbol[:3])), symbol)
    bars_n = min(max(int(request.args.get("bars") or 96), 30), 300)
    tf_raw = (request.args.get("tf") or cfg["trading"]["timeframe"]).upper()
    tf = tf_raw if tf_raw in ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1") \
        else cfg["trading"]["timeframe"]
    try:
        c = get_client()
        sym = c.resolve_symbol(fallback)
        bars = c.copy_bars(sym, tf, bars_n)
        from agents import quant as Q
        df = Q.enrich(Q.to_frame(bars), cfg["agents"])
        tail = df.tail(bars_n)
        info = c.prepare(sym)
        digits2 = info.digits

        def f(col):
            return [round(float(v), digits2) for v in tail[col]]

        payload = {
            "symbol": sym, "timeframe": tf,
            "digits": digits2,
            "candles": [[int(t.timestamp()), round(float(r["open"]), digits2),
                         round(float(r["high"]), digits2),
                         round(float(r["low"]), digits2),
                         round(float(r["close"]), digits2)]
                        for t, r in tail.iterrows()],
            "volume": [max(1.0, float(v)) for v in tail.get("tick_volume", tail.get("volume", [1] * len(tail)))],
            "ema_fast": f("ema_fast"), "ema_slow": f("ema_slow"),
            "rsi": [round(float(v), 1) for v in tail["rsi"]],
            "atr": round(float(tail["atr"].iloc[-1]), digits2),
        }
        try:                       # Kronos forecast overlay for the selected TF
            with open(LIVE_FILE, encoding="utf-8") as fh:
                entry = (json.load(fh).get("symbols") or {}).get(sym) or {}
            fc, summary, ood = None, None, False
            blob = (entry.get("forecasts") or {}).get(tf)
            if isinstance(blob, dict):
                fc = blob.get("candles")
                summary = blob.get("summary")
                ood = bool(blob.get("ood"))
            elif isinstance(blob, list):
                fc = blob
            if not fc and tf == cfg["trading"]["timeframe"]:      # legacy board
                fc = entry.get("forecast_candles")
                summary = entry.get("kronos_summary")
            if fc:
                payload["forecast"] = [[int(t), o, h, l, c] for t, o, h, l, c in fc]
                payload["forecast_summary"] = summary
                payload["forecast_ood"] = ood
        except Exception:
            pass
        return jsonify(payload)
    except Exception as exc:
        _client_cache.pop("c", None)
        return jsonify({"error": str(exc)[:160]})


@app.post("/api/start")
def api_start() -> Response:
    mode = (request.get_json(silent=True) or {}).get("mode", "dry")
    if mode not in ("dry", "live"):
        return jsonify({"ok": False, "error": "bad mode"})
    return jsonify(start_runner(mode))


@app.post("/api/stop")
def api_stop() -> Response:
    return jsonify(stop_runner())


@app.post("/api/reset_guardian")
def api_reset() -> Response:
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    open(os.path.join(ROOT, "state", "reset_guardian.flg"), "w").close()
    return jsonify({"ok": True})


# --------------------------------------------------------------- front end
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>TradingAgents Desk</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
 :root{
   --bg:#070b12; --bg2:#0a101c; --glass:rgba(20,29,47,.62); --glass2:rgba(10,16,28,.78);
   --edge:rgba(148,163,184,.14); --edge2:rgba(148,163,184,.24);
   --tx:#e9eff9; --dim:#8a9bb1; --dim2:#64748b;
   --green:#34d399; --red:#fb7185; --amber:#fbbf24; --blue:#38bdf8;
   --violet:#a78bfa; --cyan:#22d3ee; --teal:#5eead4;
   --grad:linear-gradient(120deg,#5eead4,#38bdf8 45%,#a78bfa);
   --shadow:0 24px 48px -28px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.045);
 }
 *{box-sizing:border-box}
 html{scroll-behavior:smooth}
 body{margin:0;min-height:100vh;color:var(--tx);
   font:14px/1.55 "Inter","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
   background:
     radial-gradient(900px 420px at 12% -8%, rgba(94,234,212,.13), transparent 60%),
     radial-gradient(1100px 520px at 88% -12%, rgba(167,139,250,.13), transparent 62%),
     radial-gradient(800px 500px at 55% 108%, rgba(56,189,248,.07), transparent 60%),
     linear-gradient(180deg,var(--bg),var(--bg2) 55%,#070b12);
   background-attachment:fixed;
   -webkit-font-smoothing:antialiased}
 body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
   background-image:linear-gradient(rgba(148,163,184,.045) 1px,transparent 1px),
                    linear-gradient(90deg,rgba(148,163,184,.045) 1px,transparent 1px);
   background-size:56px 56px;
   mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 20%,transparent 75%);
   -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 20%,transparent 75%)}
 .wrap{position:relative;z-index:1;max-width:1540px;margin:0 auto;padding:0 22px 42px}
 h3{margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;
   color:var(--dim);display:flex;align-items:center;gap:9px;flex-wrap:wrap}
 h3 .live-pill{font-size:9px;letter-spacing:.8px;background:rgba(34,211,238,.10);color:var(--cyan);
   border:1px solid rgba(34,211,238,.28);padding:2px 9px;border-radius:20px}
 h3 .updated{font-weight:500;letter-spacing:.2px;text-transform:none;color:var(--dim2);font-size:11px;
   margin-left:auto}
 .dim2{color:var(--dim2)} .dimc{color:var(--dim)}
 .pos{color:var(--green)} .neg{color:var(--red)}

 /* ---------- header ---------- */
 header.top{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:10px;
   padding:11px 22px;flex-wrap:wrap;border-bottom:1px solid var(--edge);
   background:rgba(7,11,18,.74);backdrop-filter:blur(20px) saturate(150%);
   -webkit-backdrop-filter:blur(20px) saturate(150%)}
 .brand{display:flex;align-items:center;gap:11px;margin-right:2px}
 .logo{width:32px;height:32px;border-radius:10px;display:inline-flex;align-items:center;
   justify-content:center;font-size:16px;color:#04121a;font-weight:900;
   background:var(--grad);box-shadow:0 8px 24px -8px rgba(56,189,248,.8)}
 .brand h1{font-size:16px;margin:0;font-weight:800;letter-spacing:.4px;white-space:nowrap}
 .brand h1 b{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
 .brand small{display:block;color:var(--dim2);font-size:9.5px;letter-spacing:2.2px;font-weight:700;
   text-transform:uppercase;margin-top:-2px}
 .badge{padding:5px 14px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:1px;
   border:1px solid var(--edge);color:var(--dim);background:rgba(10,16,28,.6)}
 .b-stop{color:var(--dim)} .b-dry{color:var(--amber);border-color:rgba(251,191,36,.4);background:rgba(69,26,3,.35)}
 .b-live{color:#6ee7b7;border-color:rgba(52,211,153,.45);background:rgba(6,46,32,.5);
   box-shadow:0 0 18px -4px rgba(52,211,153,.5)}
 .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
 .dot.on{background:var(--green);box-shadow:0 0 10px rgba(52,211,153,.8)}
 .dot.off{background:var(--red);box-shadow:0 0 10px rgba(251,113,133,.8)}
 .hstat{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dim);white-space:nowrap}
 .hstat b{color:var(--tx);font-weight:700}
 .pill{font-size:11px;color:var(--dim);border:1px solid var(--edge);padding:5px 11px;
   border-radius:9px;background:rgba(10,16,28,.55);white-space:nowrap}
 .grow{flex:1}
 button{background:linear-gradient(180deg,rgba(30,41,59,.9),rgba(15,23,42,.9));color:var(--tx);
   border:1px solid var(--edge2);border-radius:10px;padding:8px 15px;cursor:pointer;
   font:700 12px/1 "Inter","Segoe UI",sans-serif;letter-spacing:.2px;transition:.16s}
 button:hover{border-color:rgba(56,189,248,.55);transform:translateY(-1px);
   box-shadow:0 10px 22px -12px rgba(56,189,248,.65)}
 button:active{transform:translateY(0)}
 button.go{border-color:rgba(52,211,153,.35);color:#6ee7b7;background:rgba(6,46,32,.45)}
 button.warn{border-color:rgba(251,113,133,.3);color:#fda4af}
 button.mini{padding:5px 10px;font-size:11px;border-radius:8px}
 .banner{display:none;padding:11px 22px;color:#fecdd3;font-weight:700;font-size:12.5px;
   background:repeating-linear-gradient(45deg,rgba(69,10,10,.95),rgba(69,10,10,.95) 12px,rgba(92,16,16,.95) 12px,rgba(92,16,16,.95) 24px);
   border-bottom:1px solid rgba(251,113,133,.35)}

 /* ---------- cards ---------- */
 .card{position:relative;border-radius:18px;padding:18px 19px;border:1px solid var(--edge);
   background:linear-gradient(158deg,var(--glass),var(--glass2));
   box-shadow:var(--shadow);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
   transition:border-color .2s, transform .2s}
 .card:hover{border-color:rgba(148,163,184,.24)}
 .card::before{content:'';position:absolute;inset:0 18px auto;height:1px;border-radius:18px;
   background:linear-gradient(90deg,transparent,rgba(255,255,255,.14),transparent);pointer-events:none}
 .grid{display:grid;gap:13px;margin-top:14px;grid-template-columns:repeat(auto-fit,minmax(205px,1fr))}
 .kpi .l{color:var(--dim);font-size:10px;letter-spacing:1.4px;text-transform:uppercase;
   display:flex;align-items:center;gap:7px;font-weight:700}
 .kpi .v{font-size:27px;font-weight:800;letter-spacing:-.4px;font-variant-numeric:tabular-nums;
   margin-top:3px;line-height:1.15}
 .kpi small{display:block;color:var(--dim2);margin-top:3px;font-size:11px}
 .kpi-big .v{font-size:31px}
 .kpi::after{content:'';position:absolute;left:19px;right:19px;bottom:0;height:2px;border-radius:2px;
   background:linear-gradient(90deg,var(--acc,#38bdf8),transparent 75%);opacity:.55}
 .k-b{--acc:#38bdf8} .k-v{--acc:#a78bfa} .k-p{--acc:#34d399} .k-n{--acc:#fb7185}
 .k-a{--acc:#fbbf24} .k-c{--acc:#22d3ee}

 /* ---------- ticker ---------- */
 .ticker{display:flex;align-items:center;gap:14px;margin-top:16px;padding:11px 16px;border-radius:14px}
 .tk-label{display:flex;align-items:center;gap:8px;font-size:9.5px;font-weight:900;letter-spacing:1.8px;
   color:var(--cyan);white-space:nowrap}
 .tk-dot{width:7px;height:7px;border-radius:50%;background:var(--cyan);animation:blink 2s infinite}
 @keyframes blink{0%,58%{opacity:1;box-shadow:0 0 0 0 rgba(34,211,238,.5)}
   70%,100%{opacity:.25;box-shadow:0 0 0 6px rgba(34,211,238,0)}}
 .tk-scroll{display:flex;gap:8px;overflow-x:auto;flex:1;padding:2px 2px 4px;scrollbar-width:thin}
 .tk-chip{display:inline-flex;gap:7px;align-items:center;flex:0 0 auto;padding:4px 12px;border-radius:999px;
   border:1px solid var(--edge);background:rgba(10,16,28,.6);color:var(--dim);font-size:11px;
   white-space:nowrap}
 .tk-chip b{color:var(--tx);font-weight:700;font-variant-numeric:tabular-nums}
 .tk-chip small{color:var(--dim2)}
 .tk-chip.fill{border-color:rgba(52,211,153,.4);color:#a7f3d0;background:rgba(6,46,32,.35)}
 .tk-chip.close{border-color:rgba(163,230,53,.32);color:#d9f99d}
 .tk-chip.reject{border-color:rgba(251,113,133,.4);color:#fecdd3;background:rgba(69,10,10,.3)}
 .tk-chip.sl{border-color:rgba(251,191,36,.35);color:#fde68a}
 .tk-chip.partial{border-color:rgba(94,234,212,.4);color:#99f6e4;background:rgba(19,78,74,.3)}
 .tk-chip.fresh{animation:pop .9s ease-out}
 @keyframes pop{0%{transform:scale(.86)}45%{transform:scale(1.06);box-shadow:0 0 20px rgba(52,211,153,.35)}
   100%{transform:scale(1)}}

 /* ---------- layout ---------- */
 .main{display:grid;grid-template-columns:minmax(0,2.15fr) minmax(300px,1fr);gap:13px;margin-top:14px}
 .rail{display:flex;flex-direction:column;gap:13px;min-width:0}
 @media (max-width:1080px){ .main{grid-template-columns:1fr} }

 /* ---------- chart ---------- */
 .chart-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:3px}
 .chart-head h3{margin:0}
 select{background:rgba(15,23,42,.9);color:var(--tx);border:1px solid var(--edge2);
   border-radius:9px;padding:7px 11px;font:700 12px "Inter","Segoe UI",sans-serif}
 .tfbar{display:flex;gap:6px;margin:11px 0 9px;flex-wrap:wrap}
 .tftab{padding:5px 14px;border-radius:8px;border:1px solid var(--edge);cursor:pointer;
   font-size:11px;font-weight:800;letter-spacing:.4px;color:var(--dim);background:rgba(12,20,36,.7);
   user-select:none;transition:.15s}
 .tftab:hover{border-color:var(--edge2);color:var(--tx)}
 .tftab.on{color:#031018;border-color:transparent;background:var(--grad);
   box-shadow:0 8px 20px -10px rgba(56,189,248,.8)}
 .legend{display:flex;gap:15px;color:var(--dim);font-size:10.5px;align-items:center;flex-wrap:wrap}
 .legend i{width:13px;height:2.5px;border-radius:2px;display:inline-block;vertical-align:middle;margin-right:5px}
 canvas{width:100%;display:block}

 /* ---------- strategy / funnel / gauges ---------- */
 .strat{display:flex;gap:9px;align-items:baseline;padding:7px 0;border-bottom:1px dashed rgba(148,163,184,.10)}
 .strat:last-child{border-bottom:0}
 .strat b{min-width:84px;text-transform:capitalize;font-size:12.5px;font-weight:700}
 .strat .w{color:var(--violet);font-weight:800;font-variant-numeric:tabular-nums;font-size:11.5px}
 .strat .d{color:var(--dim2);font-size:11px;line-height:1.35}
 .gauge{margin:8px 0 4px}
 .gauge .gl{display:flex;justify-content:space-between;font-size:10.5px;color:var(--dim);margin-bottom:4px}
 .gauge .gl b{color:var(--tx);font-weight:700}
 .gauge .bar{height:6px;border-radius:6px;background:rgba(15,23,42,.9);overflow:hidden}
 .gauge .fillb{height:100%;border-radius:6px;transition:width .7s cubic-bezier(.4,0,.2,1);
   background:linear-gradient(90deg,#34d399,#a3e635)}
 .gauge .fillb.warn{background:linear-gradient(90deg,#fbbf24,#fb923c)}
 .gauge .fillb.hot{background:linear-gradient(90deg,#fb7185,#e11d48)}
 .funnel{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;font-size:12px}
 .funnel .fi{display:flex;justify-content:space-between;align-items:center;
   border-bottom:1px dashed rgba(148,163,184,.10);padding:5px 0}
 .funnel .fi:last-child{border-bottom:0}
 .funnel .fi b{font-size:14px;font-variant-numeric:tabular-nums}
 .funnel .fl{display:flex;align-items:center;gap:6px;color:var(--dim)}

 /* ---------- trade plan ---------- */
 .plan-card{margin-top:13px}
 .plan-meta{display:flex;gap:7px;flex-wrap:wrap;margin-left:auto}
 .tag{font-size:9.5px;letter-spacing:.7px;font-weight:800;padding:3px 9px;border-radius:20px;
   border:1px solid var(--edge);color:var(--dim);background:rgba(10,16,28,.5)}
 .tag.acc{color:#99f6e4;border-color:rgba(94,234,212,.35);background:rgba(19,78,74,.25)}
 .plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(248px,1fr));gap:10px;margin-top:13px}
 .plan{position:relative;border-radius:14px;padding:13px 14px 12px;border:1px solid var(--edge);
   background:linear-gradient(165deg,rgba(30,41,59,.55),rgba(10,16,28,.6));overflow:hidden;
   transition:.18s}
 .plan:hover{transform:translateY(-2px);border-color:var(--edge2)}
 .plan::before{content:'';position:absolute;left:0;top:12px;bottom:12px;width:3px;border-radius:3px;
   background:var(--tone,#64748b)}
 .plan.good{--tone:#34d399} .plan.live{--tone:#22d3ee} .plan.warn{--tone:#fbbf24} .plan.idle{--tone:#475569}
 .plan-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-left:5px}
 .plan-top b{font-size:14px;letter-spacing:.4px}
 .side{font-size:10px;font-weight:900;letter-spacing:.8px;padding:3px 9px;border-radius:6px;
   border:1px solid var(--edge);color:var(--dim)}
 .side.long{color:#6ee7b7;border-color:rgba(52,211,153,.4);background:rgba(6,46,32,.35)}
 .side.short{color:#fda4af;border-color:rgba(251,113,133,.4);background:rgba(69,10,10,.3)}
 .plan-price{display:flex;align-items:baseline;gap:8px;margin:9px 0 8px 5px}
 .plan-price .lbl{font-size:9px;letter-spacing:1.3px;color:var(--dim2);font-weight:800}
 .plan-price b{font-size:20px;font-weight:800;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
 .plan-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px 12px;margin-left:5px}
 .plan-grid div{display:flex;justify-content:space-between;gap:6px;font-size:11.5px;
   border-bottom:1px dashed rgba(148,163,184,.09);padding:3px 0}
 .plan-grid span{color:var(--dim2)} .plan-grid b{font-variant-numeric:tabular-nums;font-weight:700}
 .plan-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:9px 0 0 5px}
 .rr{font-size:10px;color:var(--dim);font-weight:800;letter-spacing:.6px;
   border:1px solid var(--edge);border-radius:6px;padding:2px 7px}
 .st{font-size:9px;font-weight:900;letter-spacing:1px;padding:3px 9px;border-radius:20px;
   border:1px solid var(--edge);color:var(--dim);white-space:nowrap}
 .st.good{color:#6ee7b7;border-color:rgba(52,211,153,.4);background:rgba(6,46,32,.35)}
 .st.warn{color:#fde68a;border-color:rgba(251,191,36,.38);background:rgba(69,26,3,.3)}
 .st.live{color:#67e8f9;border-color:rgba(34,211,238,.4);background:rgba(8,51,68,.4);
   animation:pulse 2.4s infinite}
 .st.idle{color:var(--dim2)}
 @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(34,211,238,.0)}
   50%{box-shadow:0 0 14px -2px rgba(34,211,238,.55)}}
 .plan-detail{margin:7px 0 0 5px;font-size:10.5px;color:var(--dim2);line-height:1.4}
 .plan-htf{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 0 5px}
 .htfchip{font-size:9.5px;font-weight:800;letter-spacing:.4px;padding:2px 7px;border-radius:5px;
   border:1px solid var(--edge);color:var(--dim2)}
 .htfchip.up{color:#6ee7b7;border-color:rgba(52,211,153,.3)}
 .htfchip.down{color:#fda4af;border-color:rgba(251,113,133,.3)}
 .htfchip small{opacity:.7;font-weight:600}

 /* ---------- kronos scorecard ---------- */
 .ks-card{margin-top:13px}
 .ks-grid{display:grid;grid-template-columns:230px minmax(0,1fr);gap:18px;align-items:center}
 @media (max-width:760px){ .ks-grid{grid-template-columns:1fr} }
 .ks-dial{position:relative;width:170px;height:170px;margin:0 auto}
 .ks-dial svg{transform:rotate(-90deg);width:170px;height:170px}
 .ks-dial .track{fill:none;stroke:rgba(148,163,184,.12);stroke-width:9}
 .ks-dial .prog{fill:none;stroke:url(#ksgrad);stroke-width:9;stroke-linecap:round;
   stroke-dasharray:326.7;stroke-dashoffset:326.7;transition:stroke-dashoffset .9s ease}
 .ks-dial .mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
   justify-content:center;text-align:center}
 .ks-dial .big{font-size:35px;font-weight:800;letter-spacing:-1px;font-variant-numeric:tabular-nums;line-height:1}
 .ks-dial .sub{font-size:9.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim2);
   font-weight:800;margin-top:5px}
 .ks-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
 .ks-stat{border:1px solid var(--edge);border-radius:10px;padding:8px 11px;background:rgba(10,16,28,.4)}
 .ks-stat .l{font-size:9px;letter-spacing:1.1px;text-transform:uppercase;color:var(--dim2);font-weight:800}
 .ks-stat .v{font-size:16px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:2px}
 .ks-note{font-size:10.5px;color:var(--dim2);margin-top:9px;line-height:1.5}
 .ks-tfs{display:grid;gap:7px;margin-top:13px}
 .ks-tf{display:grid;grid-template-columns:48px 1fr 46px 38px;gap:9px;align-items:center;font-size:11px}
 .ks-tf .bar{height:7px;border-radius:6px;background:rgba(15,23,42,.9);overflow:hidden}
 .ks-tf .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#5eead4,#38bdf8);
   transition:width .8s}
 .ks-tf .fill.low{background:linear-gradient(90deg,#fb7185,#fbbf24)}
 .ks-tf .pct{font-weight:800;font-variant-numeric:tabular-nums;text-align:right}
 .ks-tf .n{color:var(--dim2);text-align:right;font-size:10px}
 .ks-recent{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
 .ks-chip{display:inline-flex;gap:5px;align-items:center;font-size:10px;padding:3px 8px;border-radius:6px;
   border:1px solid var(--edge);color:var(--dim);background:rgba(10,16,28,.4)}
 .ks-chip.hit{border-color:rgba(52,211,153,.3);color:#a7f3d0}
 .ks-chip.miss{border-color:rgba(251,113,133,.3);color:#fecdd3}
 .ks-chip b{font-weight:800}

 /* ---------- tables ---------- */
 .tablewrap{overflow-x:auto;margin:0 -4px}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th{color:var(--dim2);text-align:left;padding:7px 8px;border-bottom:1px solid var(--edge);
   font-size:9px;letter-spacing:1.2px;text-transform:uppercase;font-weight:800;white-space:nowrap}
 td{padding:7px 8px;border-bottom:1px solid rgba(148,163,184,.08);font-variant-numeric:tabular-nums;
   vertical-align:top}
 tbody tr{transition:background .12s}
 tbody tr:hover{background:rgba(148,163,184,.045)}
 tbody tr:last-child td{border-bottom:0}
 .arrow{font-size:13px;font-weight:900}
 .chip{display:inline-block;border:1px solid var(--edge);border-radius:5px;padding:1px 6px;margin:1px 2px;
   font-size:10px;color:var(--dim);cursor:default;white-space:nowrap}
 .chip.u{color:#6ee7b7;border-color:rgba(52,211,153,.3);background:rgba(6,46,32,.25)}
 .chip.d{color:#fda4af;border-color:rgba(251,113,133,.3);background:rgba(69,10,10,.25)}
 .chip.news{color:#fecdd3;border-color:rgba(251,113,133,.3);background:rgba(69,10,10,.35);
   cursor:help;white-space:normal}
 .chip.tf{cursor:pointer}
 .chip.tf:hover{border-color:var(--blue)}
 .chip.ens{color:#99f6e4;border-color:rgba(94,234,212,.3)}
 .regime{font-weight:800} .regime.ok{color:var(--green)} .regime.dead{color:var(--red)}
 .tablewrap table{min-width:700px}
 pre{margin:0;font:11.5px/1.5 "JetBrains Mono",Consolas,monospace;color:#9fb2c6;max-height:260px;
   overflow:auto;white-space:pre-wrap;background:rgba(3,7,14,.45);border:1px solid var(--edge);
   border-radius:10px;padding:11px}
 .lower{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(300px,1fr);gap:13px;margin-top:13px}
 @media (max-width:1080px){ .lower{grid-template-columns:1fr} }
 .stack{display:flex;flex-direction:column;gap:13px}
 .foot{color:var(--dim2);font-size:10.5px;text-align:center;padding:20px 0 0;line-height:1.6}
 .foot b{color:var(--dim)}
 @keyframes kpiFlash{0%{box-shadow:0 0 0 rgba(52,211,153,0)}35%{box-shadow:0 0 30px -4px rgba(52,211,153,.45)}
   100%{box-shadow:var(--shadow)}}
 .flashcard{animation:kpiFlash 1.5s ease-out}
 @media (max-width:620px){
   .wrap{padding:0 12px 30px} header.top{padding:9px 12px;gap:7px}
   .brand h1{font-size:14px} .kpi .v{font-size:22px} .kpi-big .v{font-size:25px}
   .grid{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:9px}
   .card{padding:14px;border-radius:15px}
   .ticker{margin-top:12px}
   .hstat{display:none}
 }
</style></head>
<body>
<header class="top">
 <div class="brand">
   <span class="logo">&#9672;</span>
   <h1>TradingAgents <b>DESK</b><small>multi-agent execution</small></h1>
 </div>
 <span id="mode" class="badge b-stop">STOPPED</span>
 <span class="hstat"><span class="dot off" id="mt5dot"></span><b id="mt5label">connecting&#8230;</b></span>
 <span class="pill" id="cfgline"></span>
 <span class="grow"></span>
 <span class="pill" id="autosync">&#8635; &hellip;</span>
 <span class="pill" id="clock">&ndash;:&ndash;&ndash;</span>
 <button class="mini" onclick="resetGuardian()">&#8635; guardian</button>
 <button class="go" onclick="startRunner('dry')">&#9654; DRY</button>
 <button onclick="startRunner('live')">&#9654; LIVE</button>
 <button class="warn" onclick="stopRunner()">&#9632; STOP</button>
</header>
<div id="haltbanner" class="banner">GUARDIAN LOCKOUT &#8212; trading halted (risk limit hit). Reset guardian above.</div>

<div class="wrap">
 <div class="card ticker">
   <span class="tk-label"><span class="tk-dot"></span>LIVE FEED</span>
   <div class="tk-scroll" id="ticker"><span class="tk-chip">quiet&#8230; waiting for broker events</span></div>
 </div>

 <div class="grid">
  <div class="card kpi k-b"><div class="l"><span>&#9679;</span>Equity</div><div class="v" id="equity">&#8212;</div><small id="equitysub"></small></div>
  <div class="card kpi k-v"><div class="l"><span>&#9679;</span>Balance</div><div class="v" id="balance">&#8212;</div></div>
  <div class="card kpi k-n"><div class="l"><span>&#9679;</span>Today's bot P/L</div><div class="v" id="daily">&#8212;</div><small id="flush"></small></div>
  <div class="card kpi k-p"><div class="l"><span>&#9679;</span>Floating P/L</div><div class="v" id="floating">&#8212;</div></div>
  <div class="card kpi k-a"><div class="l"><span>&#9679;</span>Free margin</div><div class="v" id="freemargin">&#8212;</div><small id="mlsub"></small></div>
  <div class="card kpi k-c"><div class="l"><span>&#9679;</span>Decisions / Filled</div><div class="v" id="counts">&#8212;</div><small id="llmstate"></small></div>
  <div class="card"><div class="l" style="color:var(--dim);font-size:10px;letter-spacing:1.4px;text-transform:uppercase;font-weight:700">
    &#128737; Risk guard</div>
   <div class="gauge"><div class="gl"><span>Day-loss guard used</span><b id="gdaytxt">0%</b></div>
     <div class="bar"><div class="fillb" id="gday" style="width:0%"></div></div></div>
   <div class="gauge"><div class="gl"><span>Drawdown from peak</span><b id="gddtxt">0%</b></div>
     <div class="bar"><div class="fillb" id="gdd" style="width:0%"></div></div></div>
   <small id="gpeaks" class="dim2" style="display:block;margin-top:5px;font-size:10px"></small>
  </div>
 </div>

 <div class="main">
  <div class="card" id="chartcard">
    <div class="chart-head">
      <h3 style="margin:0">Market analysis <span class="live-pill">KRONOS FORECAST</span></h3>
      <select id="symsel" onchange="loadChart()"></select>
    </div>
    <div class="tfbar" id="tfsel"></div>
    <div class="legend" style="margin:0 0 7px">
      <span><i style="background:#38bdf8"></i>EMA fast</span>
      <span><i style="background:#a78bfa"></i>EMA slow</span>
      <span><i style="background:repeating-linear-gradient(90deg,#fbbf24 0 4px,transparent 4px 7px)"></i>Kronos forecast</span>
      <span id="chartmeta" style="margin-left:auto"></span>
    </div>
    <canvas id="chart" height="330"></canvas>
  </div>
  <div class="rail">
   <div class="card">
     <h3>Strategy desk <span class="updated">weighted votes</span></h3>
     <div id="strats"></div>
   </div>
   <div class="card">
     <h3>Equity curve <span class="live-pill" id="eqmeta"></span></h3>
     <canvas id="eqspark" height="120"></canvas>
   </div>
   <div class="card">
     <h3>Today's committee</h3>
     <div class="funnel" id="funnel">
       <div class="fi"><span class="fl">&#9989; Filled</span><b class="pos" id="f_filled">&#8212;</b></div>
       <div class="fi"><span class="fl">&#128683; LLM veto</span><b class="neg" id="f_vetoed">&#8212;</b></div>
       <div class="fi"><span class="fl">&#128679; Risk blocks</span><b id="f_blocked">&#8212;</b></div>
       <div class="fi"><span class="fl">&#128164; Flat (no trade)</span><b class="dimc" id="f_flat">&#8212;</b></div>
     </div>
     <small class="dim2" style="display:block;margin-top:8px;font-size:10px;line-height:1.5">fills include pass-throughs · blocks = guard gates (spread, regime, news, positions)</small>
   </div>
  </div>
 </div>

 <div class="card plan-card">
   <h3 style="margin-bottom:0">Trade plan <span class="live-pill">NEXT ENTRY</span>
     <span class="plan-meta" id="planmeta"></span></h3>
   <div class="plans" id="plans"></div>
 </div>

 <div class="card ks-card">
   <h3>Kronos accuracy <span class="live-pill">SELF-GRADING</span>
     <span class="updated" id="ksmeta"></span></h3>
   <div class="ks-grid">
     <div class="ks-dial">
       <svg viewBox="0 0 170 170">
         <defs><linearGradient id="ksgrad" x1="0" y1="0" x2="1" y2="1">
           <stop offset="0" stop-color="#5eead4"/><stop offset=".55" stop-color="#38bdf8"/>
           <stop offset="1" stop-color="#a78bfa"/></linearGradient></defs>
         <circle class="track" cx="85" cy="85" r="52"/>
         <circle class="prog" id="ksring" cx="85" cy="85" r="52"/>
       </svg>
       <div class="mid"><div class="big" id="ksrate">&#8212;</div><div class="sub">next-bar hit rate</div></div>
     </div>
     <div>
       <div class="ks-stats">
         <div class="ks-stat"><div class="l">Graded calls</div><div class="v" id="kscalls">&#8212;</div></div>
         <div class="ks-stat"><div class="l">Last 20 form</div><div class="v" id="ksform">&#8212;</div></div>
         <div class="ks-stat"><div class="l">Avg realized</div><div class="v" id="ksreal">&#8212;</div></div>
         <div class="ks-stat"><div class="l">Avg error</div><div class="v" id="kserr">&#8212;</div></div>
       </div>
       <div class="ks-tfs" id="kstfs"></div>
       <div class="ks-recent" id="ksrecent"></div>
       <div class="ks-note" id="ksnote">Every forecast is graded against the next closed bar. A coin-flip model scores 50% here &#8212; the scorecard tells you whether Kronos is earning its vote.</div>
     </div>
   </div>
 </div>

 <div class="grid" style="margin-top:13px">
  <div class="card" style="grid-column:1/-1"><h3>Signal board <span class="updated" id="boardage"></span></h3>
    <div class="tablewrap"><table id="board"><thead><tr>
      <th>Symbol</th><th>Last</th><th>ATR</th><th>Spread</th><th>Regime</th>
      <th>Agents</th><th>Kronos outlook</th><th>Consensus</th><th>AI verdict</th><th>Status</th>
    </tr></thead><tbody></tbody></table></div></div>
 </div>

 <div class="lower">
   <div class="stack">
     <div class="card"><h3>Open bot positions</h3>
       <div class="tablewrap"><table id="pos"><thead><tr>
         <th>ID</th><th>Side</th><th>Symbol</th><th>Volume</th><th>Entry</th>
         <th>SL</th><th>TP</th><th>P/L</th></tr></thead><tbody></tbody></table></div></div>
     <div class="card"><h3>Decision journal <span class="updated" id="jstat"></span></h3>
       <div class="tablewrap"><table id="dec"><thead><tr><th>Time</th><th>Symbol</th><th>Action</th>
         <th>Dir</th><th>Vol</th><th>Strength</th><th>Conf</th><th>LLM</th><th>Exec</th><th>Reason</th>
       </tr></thead><tbody></tbody></table></div></div>
   </div>
   <div class="card"><h3>Live log</h3><pre id="log">&#8230;</pre></div>
 </div>

 <div class="foot">TradingAgents Desk &#183; local only &#183; every value is pulled live from MT5 and the runner<br>
   <b>Risk first.</b> A model that is right 54% of the time still needs discipline to make money.</div>
</div>

<script>
const $ = id => document.getElementById(id);
const DASH = '\u2014';
const fmt = (v,d=2) => v==null||isNaN(v) ? DASH : (+v).toLocaleString(undefined,{maximumFractionDigits:d});
const px = v => v==null||isNaN(v) ? DASH : (+v>=20 ? (+v).toFixed(2) : (+v).toFixed(5));
const spreadTxt = v => v==null||isNaN(v)||Math.abs(v)>1000 ? DASH : (+v).toFixed(1);
const cls = v => v>=0 ? 'pos':'neg';
const sign = v => v==null||isNaN(v) ? DASH : (v>0?'+':'')+(+v).toFixed(2);
const pct = v => v==null||isNaN(v) ? DASH : (v>0?'+':'')+(+v).toFixed(2)+'%';
let chartData=null, chartSymbol='', chartTF='M15', chartLoading=false, KRONOS_TFS=['M15'];
window.DEAD_RATIO = 0.62;

function buildTfTabs(){
  const bar=$('tfsel'); if(!bar) return; bar.innerHTML='';
  (KRONOS_TFS.length?KRONOS_TFS:['M15']).forEach(tf=>{
    const d=document.createElement('div');
    d.className='tftab'+(tf===chartTF?' on':'');
    d.textContent=tf;
    d.onclick=()=>{ chartTF=tf; buildTfTabs(); loadChart(); };
    bar.appendChild(d);
  });
}

function drawCandles(){
  const cv=$('chart'); if(!cv) return; const c=cv.getContext('2d');
  cv.width=cv.clientWidth;
  const H=cv.height,W=cv.width;
  c.clearRect(0,0,W,H);
  c.fillStyle='#7f8ea3'; c.font='12px "Segoe UI",sans-serif';
  if(!chartData || !chartData.candles || chartData.candles.length<3){
    c.fillText('waiting for MT5 data \u2014 open and log into the terminal', 12, 30); return; }
  const K=chartData.candles, d=chartData.digits, V=chartData.volume||[];
  const F=chartData.forecast||[];
  const n=K.length, m=F.length;
  const hi0=Math.max(...K.map(k=>k[2])), lo0=Math.min(...K.map(k=>k[3]));
  const hi=Math.max(hi0, ...F.map(k=>k[2])), lo=Math.min(lo0, ...F.map(k=>k[3]));
  const pad=(hi-lo)*0.08||1e-6, top=hi+pad, bot=lo-pad;
  const L=8,R=64,T=12,B=24;
  const Y=v=>T+(top-v)/(top-bot)*(H-T-B);
  const cw=(W-L-R)/(n+m);
  c.strokeStyle='rgba(148,163,184,.10)'; c.fillStyle='#6b7c93'; c.font='10px Consolas,monospace';
  for(let i=0;i<=4;i++){ const v=bot+(top-bot)*i/4, y=Y(v);
    c.beginPath(); c.moveTo(L,y); c.lineTo(W-R,y); c.stroke();
    c.fillText(v.toFixed(d), W-58, y+3); }
  const intraday=chartTF.charAt(0)==='M'||chartTF==='H1';
  const tstep=Math.max(1,Math.round((n+m)/5));
  for(let i=0;i<n;i+=tstep){
    c.fillStyle='#55657a';
    const dt=new Date(K[i][0]*1000);
    const lbl=intraday?dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                      :dt.toLocaleDateString([],{month:'2-digit',day:'2-digit'});
    c.fillText(lbl,L+cw*i,H-8);
  }
  if(V.length){
    const vmax=Math.max(...V), bh=(H-T-B)*0.15, bw=Math.max(1.5,cw*0.55);
    V.forEach((v,i)=>{
      const x=L+cw*i+cw*0.5, up=K[i]&&K[i][4]>=K[i][1];
      c.fillStyle=up?'rgba(52,211,153,.18)':'rgba(251,113,133,.18)';
      const hb=Math.max(1,v/vmax*bh);
      c.fillRect(x-bw/2,H-B-hb,bw,hb);
    });
  }
  K.forEach((k,i)=>{
    const x=L+cw*i+cw*0.5, up=k[4]>=k[1], bw=Math.max(1.5,cw*0.55);
    c.strokeStyle=up?'#34d399':'#fb7185'; c.fillStyle=up?'rgba(6,78,59,.55)':'rgba(76,5,25,.55)';
    const yO=Y(k[1]), yC=Y(k[4]);
    const yT=Math.min(yO,yC), hgt=Math.max(Math.abs(yC-yO),1);
    c.beginPath(); c.moveTo(x,Y(k[2])); c.lineTo(x,Y(k[3])); c.stroke();
    c.fillRect(x-bw/2,yT,bw,hgt); c.strokeRect(x-bw/2,yT,bw,hgt);
  });
  if(m){
    const sepX=L+cw*n;
    c.setLineDash([4,4]); c.strokeStyle='rgba(251,191,36,.45)';
    c.beginPath(); c.moveTo(sepX,T); c.lineTo(sepX,H-B); c.stroke(); c.setLineDash([]);
    c.fillStyle='#fbbf24'; c.font='bold 10px "Segoe UI",sans-serif';
    c.fillText('Kronos forecast \u2192'+(chartData.forecast_ood?'  \u26A0 OOD':''),sepX+4,T+11);
    F.forEach((k,j)=>{
      const x=L+cw*(n+j)+cw*0.5;
      c.strokeStyle='#fbbf24'; c.setLineDash([3,2]);
      c.beginPath(); c.moveTo(x,Y(k[2])); c.lineTo(x,Y(k[3])); c.stroke();
      c.setLineDash([]);
      const yO=Y(k[1]), yC=Y(k[4]);
      const yT=Math.min(yO,yC), hgt=Math.max(Math.abs(yC-yO),1);
      c.strokeStyle=k[4]>=k[1]?'#fcd34d':'#b45309';
      c.strokeRect(x-Math.max(1.5,cw*0.55)/2,yT,Math.max(1.5,cw*0.55),hgt);
    });
  }
  const line=(arr,col)=>{ c.strokeStyle=col; c.lineWidth=1.4; c.beginPath();
    arr.forEach((v,i)=>{ const x=L+cw*i+cw*0.5, y=Y(v); i?c.lineTo(x,y):c.moveTo(x,y); });
    c.stroke(); c.lineWidth=1; };
  if(chartData.ema_fast) line(chartData.ema_fast,'#38bdf8');
  if(chartData.ema_slow) line(chartData.ema_slow,'#a78bfa');
  const last=K[n-1], lx=L+cw*n, ly=Y(last[4]);
  c.setLineDash([3,3]); c.strokeStyle='rgba(148,163,184,.5)';
  c.beginPath(); c.moveTo(L,ly); c.lineTo(W-58,ly); c.stroke(); c.setLineDash([]);
  c.fillStyle=last[4]>=last[1]?'#065f46':'#7f1d1d';
  c.fillRect(lx+2,ly-9,56,18); c.fillStyle='#fff'; c.font='bold 11px Consolas,monospace';
  c.fillText(last[4].toFixed(d),lx+8,ly+4);
  c.fillStyle='rgba(148,163,184,.18)'; c.font='bold 13px "Segoe UI",sans-serif';
  c.fillText(chartData.symbol+'  '+chartData.timeframe,L+4,T+13);
}

async function loadChart(){
  const sel=$('symsel'); const sym=sel.value||chartSymbol;
  if(!sym||chartLoading) return; chartSymbol=sym; chartLoading=true;
  try{
    const r=await fetch('/api/chart?symbol='+encodeURIComponent(sym)+'&tf='+chartTF+'&bars=110');
    const data=await r.json();
    chartData=data.error?null:data;
    $('chartmeta').textContent=data.error?('chart unavailable: '+data.error):
      ('RSI '+(chartData.rsi?chartData.rsi[chartData.rsi.length-1]:DASH)+'  \u00B7  ATR '+chartData.atr+
       (chartData.forecast_summary?'  \u00B7  \uD83E\uDD16 Kronos('+chartTF+'): '+chartData.forecast_summary
        :'  \u00B7  Kronos('+chartTF+'): preparing\u2026'));
  }catch(e){ $('chartmeta').textContent='chart fetch failed'; }
  chartLoading=false; drawCandles();
}

function chips(votes){
  const map={trend:1,momentum:1,reversion:1,breakout:1,pullback:1,regime:1,kronos:1};
  return Object.entries(votes||{}).filter(([k])=>map[k]).map(([k,v])=>
    `<span class="chip ${v>0?'u':v<0?'d':''}">${k[0]}${k==='kronos'?'\u2726':''}${v>0?'\u2191':v<0?'\u2193':'\u00B7'}</span>`
  ).join('');
}

function outlook(d){
  if(!d||!d.forecasts) return DASH;
  return ['M15','H1','H4','D1'].map(tf=>{
    const f=(d.forecasts||{})[tf];
    if(!f) return '<span class="chip">'+tf+' \u00B7</span>';
    const a=f.dir>0?'\u25B2':f.dir<0?'\u25BC':'\u00B7';
    const mark=f.ood?'\u26A0':'';
    const ens=(f.runs>1)?' <span class="chip ens" style="margin-left:0">'+Math.round((f.agreement||0)*100)+'%</span>':'';
    return `<span class="chip tf ${f.dir>0?'u':f.dir<0?'d':''}" title="${(f.summary||'').replace(/"/g,'')}"
      onclick="chartTF='${tf}';buildTfTabs();loadChart()">${tf}${mark} ${a}</span>${ens}`;
  }).join('');
}

function drawEquity(curve){
  const cv=$('eqspark'); if(!cv) return; const c=cv.getContext('2d');
  cv.width=cv.clientWidth;
  const H=cv.height,W=cv.width;
  c.clearRect(0,0,W,H);
  if(!curve||curve.length<2){ c.fillStyle='#6b7c93'; c.font='11px "Segoe UI",sans-serif';
    c.fillText('the curve draws once the bot logs decisions\u2026',10,26); return; }
  const pts=curve.map(p=>[+p[0],+p[1]]), vals=pts.map(p=>p[1]);
  const mn=Math.min(...vals), mx=Math.max(...vals);
  const pad=(mx-mn)*0.12||1, top=mx+pad, bot=mn-pad;
  const T=6,Bm=16,L=4,R=4;
  const X=i=>L+i/(pts.length-1)*(W-L-R);
  const Y=v=>T+(top-v)/(top-bot)*(H-T-Bm);
  c.strokeStyle='rgba(148,163,184,.10)';
  for(let i=0;i<=2;i++){ const v=bot+(top-bot)*i/2,y=Y(v);
    c.beginPath(); c.moveTo(L,y); c.lineTo(W-R,y); c.stroke(); }
  const g=c.createLinearGradient(0,T,0,H); g.addColorStop(0,'rgba(52,211,153,.32)'); g.addColorStop(1,'rgba(52,211,153,0)');
  c.beginPath(); c.moveTo(X(0),Y(vals[0]));
  pts.forEach((p,i)=>c.lineTo(X(i),Y(p[1])));
  c.lineTo(X(pts.length-1),H-Bm); c.lineTo(X(0),H-Bm); c.closePath(); c.fillStyle=g; c.fill();
  c.beginPath(); c.strokeStyle='#34d399'; c.lineWidth=1.6;
  pts.forEach((p,i)=>i?c.lineTo(X(i),Y(p[1])):c.moveTo(X(i),Y(p[1])));
  c.stroke();
  c.fillStyle='#8a9bb1'; c.font='9.5px Consolas,monospace';
  c.fillText('min '+fmt(mn),L+2,H-3);
  c.fillText('now '+fmt(vals[vals.length-1]),W-78,Y(vals[vals.length-1])-4);
}

// ---------- ticker ----------
let lastEventTs=0, syncSecs=0;
function tickChip(e,fresh){
  const t=new Date(e.ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  const k=e.kind||'', d=(e.detail||'').trim();
  let c='tk-chip', icon='\u2022', label='';
  if(k==='ORDER_FILLED'){c+=' fill';icon='\u26A1';label='FILL';}
  else if(k==='ORDER_CLOSED'){c+=' close';icon='\uD83C\uDFC1';label='CLOSE';}
  else if(k==='ORDER_REJECTED'){c+=' reject';icon='\uD83D\uDEAB';label='REJECT';}
  else if(k==='SL_MODIFIED'){c+=' sl';icon='\uD83D\uDEE1';label='SL \u2192';}
  else if(k==='PARTIAL_CLOSE'){c+=' partial';icon='\u2702\uFE0F';label='PARTIAL';}
  else if(k==='TIME_STOP'){c+=' close';icon='\u23F1\uFE0F';label='TIME STOP';}
  return `<span class="${c}${fresh?' fresh':''}">${icon} ${label} ${e.symbol?e.symbol+' ':''}<b>${d}</b><small>\u00B7${t}</small></span>`;
}
function renderTicker(s){
  const el=$('ticker'); if(!el) return;
  const evs=s.events||[];
  el.innerHTML=evs.length?evs.slice().reverse().map(e=>tickChip(e,lastEventTs>0&&e.ts>lastEventTs)).join('')
    :'<span class="tk-chip">quiet\u2026 waiting for broker events</span>';
  const newest=evs.length?evs[evs.length-1].ts:0;
  if(lastEventTs&&newest>lastEventTs){
    el.closest('.card').classList.add('flashcard');
    setTimeout(()=>el.closest('.card').classList.remove('flashcard'),1600);
  }
  lastEventTs=Math.max(lastEventTs,newest);
}
function renderFunnel(f){
  if(!f) return;
  const map={filled:'f_filled',vetoed:'f_vetoed',blocked:'f_blocked',flat:'f_flat'};
  Object.entries(map).forEach(([k,id])=>{const n=$(id); if(n) n.textContent=(f[k]||0);});
}
function renderGuards(s){
  const eq=(s.mt5&&s.mt5.equity)||0, daily=(s.mt5&&s.mt5.daily_pnl)||0;
  const limitPct=(s.trading_cfg&&s.trading_cfg.daily_loss_limit)||3;
  const ddLimit=(s.trading_cfg&&s.trading_cfg.max_dd_limit)||15;
  const used=eq>0?Math.max(0,-daily)/eq/(limitPct/100)*100:0;
  const peak=s.peak||eq;
  const dd=peak>0?Math.max(0,(peak-eq)/peak*100):0;
  const set=(id,pc)=>{const b=$(id); if(!b)return;
    b.style.width=Math.max(0,Math.min(100,pc))+'%';
    b.className='fillb'+(pc>85?' hot':pc>50?' warn':'');};
  set('gday',used); $('gdaytxt').textContent=used.toFixed(0)+'% of -'+limitPct+'%';
  set('gdd',dd/ddLimit*100); $('gddtxt').textContent=dd.toFixed(2)+'% / '+ddLimit+'%';
  $('gpeaks').textContent='peak '+fmt(peak)+' \u00B7 day guard -'+limitPct+'% \u00B7 max dd -'+ddLimit+'%';
}

// ---------- trade plan ----------
function planTile(p){
  const dirTxt=p.dir>0?'\u25B2 LONG':p.dir<0?'\u25BC SHORT':'\u00B7 FLAT';
  const side=p.dir>0?'long':p.dir<0?'short':'';
  const tone=p.tone||'idle';
  const htf=(p.htf||[]).map(h=>{
    const a=h.dir>0?'\u25B2':h.dir<0?'\u25BC':'\u00B7';
    const ag=(h.agree!=null&&h.agree<1)?' <small>'+Math.round(h.agree*100)+'%</small>':'';
    return `<span class="htfchip ${h.dir>0?'up':h.dir<0?'down':''}">${h.tf} ${a}${ag}</span>`;
  }).join('');
  return `<div class="plan ${tone}">
    <div class="plan-top"><b>${p.symbol}</b><span class="side ${side}">${dirTxt}</span></div>
    <div class="plan-price"><span class="lbl">ENTRY</span><b>${px(p.entry!=null?p.entry:p.price)}</b></div>
    <div class="plan-grid">
      <div><span>Stop</span><b class="neg">${px(p.sl)}</b></div>
      <div><span>Target</span><b class="pos">${px(p.tp)}</b></div>
      <div><span>Lots</span><b>${p.lots!=null?p.lots:DASH}</b></div>
      <div><span>Risk</span><b>$${fmt(p.risk_usd,0)}</b></div>
    </div>
    <div class="plan-foot"><span class="rr">R:R ${p.rr!=null?p.rr:DASH}</span>
      <span class="st ${tone}">${p.status}</span></div>
    <div class="plan-detail">${p.detail||''}</div>
    ${htf?'<div class="plan-htf">'+htf+'</div>':''}
  </div>`;
}
function renderPlans(s){
  const box=$('plans'); if(!box) return;
  const t=s.trading_cfg||{}, plans=s.plan||[];
  $('planmeta').innerHTML =
    `<span class="tag acc">risk ${t.risk_pct}% / trade</span>`+
    `<span class="tag">${t.partial_close?'partial '+t.partial_close_pct+'% at '+t.partial_close_at_r+'R':'partial off'}</span>`+
    `<span class="tag">${t.time_stop_minutes?'time stop '+t.time_stop_minutes+'m':'no time stop'}</span>`+
    `<span class="tag">${t.kronos_runs>1?'Kronos x'+t.kronos_runs:'Kronos x1'}</span>`;
  box.innerHTML=plans.length?plans.map(planTile).join('')
    :'<div class="dim2" style="grid-column:1/-1;font-size:12px">the plan appears once the runner writes its first board\u2026</div>';
}

// ---------- kronos scorecard ----------
function renderKronos(s){
  const k=s.kronos_score||{}, g=+k.graded||0, hr=k.hit_rate;
  const ring=$('ksring');
  if(ring){
    const c=326.7, off=c*(1-(hr==null?0:Math.max(0,Math.min(1,hr))));
    ring.style.strokeDashoffset=off;
  }
  $('ksrate').textContent=hr==null?DASH:Math.round(hr*100)+'%';
  $('kscalls').textContent=g?(g+' calls'):DASH;
  $('ksform').textContent=k.recent_hit_rate==null?DASH:Math.round(k.recent_hit_rate*100)+'%';
  $('ksreal').textContent=k.avg_realized_pct==null?DASH:(+k.avg_realized_pct).toFixed(3)+'%';
  $('kserr').textContent=k.avg_error_pct==null?DASH:(+k.avg_error_pct).toFixed(3)+'%';
  $('ksmeta').textContent=g?(g+' graded \u00B7 '+(k.hits||0)+' hits'):'building sample';

  const tfs=Object.entries(k.by_tf||{}).filter(([,v])=>v&&v.graded);
  $('kstfs').innerHTML=tfs.length?tfs.map(([tf,v])=>{
    const rate=v.hit_rate==null?0:v.hit_rate*100;
    return `<div class="ks-tf"><b>${tf}</b>
      <div class="bar"><div class="fill ${rate<52?'low':''}" style="width:${Math.max(2,Math.min(100,rate))}%"></div></div>
      <span class="pct">${v.hit_rate==null?DASH:Math.round(rate)+'%'}</span>
      <span class="n">${v.graded}</span></div>`;
  }).join(''):'<div class="dim2" style="font-size:11px">per-timeframe split appears after the first graded bar close\u2026</div>';

  const rec=k.recent||[];
  $('ksrecent').innerHTML=rec.length?rec.map(r=>{
    const win=r.hit===1, loss=r.hit===0;
    const icon=r.dir===0?'\u00B7':win?'\u2713':loss?'\u2717':'\u00B7';
    return `<span class="ks-chip ${win?'hit':loss?'miss':''}">
      <b>${r.symbol||''}</b> ${r.tf||''} ${icon}
      <span>${r.realized_pct==null?DASH:(+r.realized_pct).toFixed(2)+'%'}</span></span>`;
  }).join(''):'<span class="dim2" style="font-size:11px">no graded calls yet \u2014 the first grade lands after the next M15 close</span>';

  const split=k.agreement_split||{};
  if(split.unanimous&&split.unanimous.graded&&split.split&&split.split.graded){
    $('ksnote').innerHTML='When the ensemble was unanimous it hit <b class="pos">'+
      Math.round((split.unanimous.hit_rate||0)*100)+'%</b> ('+split.unanimous.graded+' calls); '+
      'split votes hit <b class="'+(split.split.hit_rate>=0.5?'pos':'neg')+'">'+
      Math.round((split.split.hit_rate||0)*100)+'%</b> ('+split.split.graded+').';
  }else{
    $('ksnote').textContent='Every forecast is graded against the next closed bar. A coin-flip model scores 50% here \u2014 the scorecard tells you whether Kronos is earning its vote.';
  }
}

async function snap(){
  let s;
  try{ s=await (await fetch('/api/snapshot')).json(); }
  catch(e){ const a=$('autosync'); if(a) a.innerHTML='\u26A0 retry'; return; }
  syncSecs=3;
  KRONOS_TFS=(s.trading_cfg.kronos_tfs&&s.trading_cfg.kronos_tfs.length)?s.trading_cfg.kronos_tfs:['M15'];
  if(!$('tfsel').children.length) buildTfTabs();
  window.DEAD_RATIO=s.trading_cfg.dead_ratio||0.62;

  const mode=s.runner.mode.toUpperCase();
  $('mode').textContent=mode;
  $('mode').className='badge '+(mode=='STOPPED'?'b-stop':mode=='DRY-RUN'?'b-dry':'b-live');

  const m=s.mt5||{}, on=!!m.connected;
  $('mt5dot').className='dot '+(on?'on':'off');
  $('mt5label').textContent=on?('\u2713 MT5 '+m.login+' \u00B7 '+(m.positions.length?m.positions.length+' pos':'flat')):'\u2717 MT5 '+(m.error||'disconnected');
  $('equity').textContent=on?fmt(m.equity):DASH;
  $('equitysub').textContent=on?('balance '+fmt(m.balance)):'';
  $('balance').textContent=on?fmt(m.balance):DASH;
  $('daily').textContent=on?sign(m.daily_pnl):DASH; $('daily').className='v '+cls(m.daily_pnl||0);
  $('floating').textContent=on?sign(m.floating):DASH; $('floating').className='v '+cls(m.floating||0);
  $('freemargin').textContent=on?fmt(m.free_margin):DASH;
  $('mlsub').textContent=on?('margin level '+m.margin_level+'%'):'';
  const lots=on?(m.positions.reduce((a,p)=>a+(+p.volume||0),0)):0;
  $('flush').textContent=on?(m.positions.length+' pos \u00B7 '+lots.toFixed(2)+' lots'):'';
  $('counts').textContent=s.stats.decisions+' / '+s.stats.filled_orders;
  $('llmstate').textContent=s.trading_cfg.llm_enabled?('AI: '+s.trading_cfg.llm_model):'AI: off';
  $('jstat').textContent=s.stats.decisions+' logged, '+s.stats.filled_orders+' filled';
  drawEquity(s.equity);
  const t=s.trading_cfg;
  $('cfgline').textContent=(t.symbols||[]).join(' \u00B7 ')+' '+t.timeframe+' \u00B7 risk '+t.risk_pct+'%';
  $('eqmeta').textContent=((s.equity||[]).length)+' pts \u00B7 '+t.timeframe;

  const sel=$('symsel'), names=t.symbols||[];
  if(sel.options.length!==names.length||sel.value===''){
    sel.innerHTML=names.map(n=>'<option>'+n+'</option>').join('');
    if(!sel.value) sel.value=chartSymbol||names[0];
  }
  if(!chartData&&sel.value) loadChart();   // first paint: symsel exists now

  $('strats').innerHTML=(s.strategies||[]).map(x=>
    `<div class="strat"><b>${x.name}</b><span class="w">w ${x.weight}</span>
     <span class="d">${x.how}</span></div>`).join('');

  const tb=$('board').tBodies[0]; tb.innerHTML='';
  const board=s.live&&s.live.symbols?s.live.symbols:{};
  let age=s.live?s.live.age:null;
  $('boardage').textContent=(age==null)?'(idle)':(age<60?age+'s ago':Math.round(age/60)+'m ago');
  for(const sym of (t.symbols||[])){
    const d=board[sym];
    const dir=d?(d.dir>0?'<span class="arrow pos">\u25B2 LONG</span>':d.dir<0?'<span class="arrow neg">\u25BC SHORT</span>':'\u00B7 flat'):DASH;
    const regime=d&&d.regime!=null?`<span class="regime ${d.regime>window.DEAD_RATIO?'ok':'dead'}">${d.regime}</span>`:DASH;
    const ai=d?(d.llm?(d.llm+' <span class="chip">'+(d.llm_comment||'').slice(0,34)+'</span>'):'no consensus'):DASH;
    const act=d?(d.action||d.why||''):'';
    const htf=d&&d.htf?` <span class="chip ens" title="H1/H4 confluence ${d.htf.applied>0?'+':''}${d.htf.applied}">\u26A1 ${d.htf.votes}v</span>`:'';
    const news=d&&d.news?`<span class="chip news" title="${d.news.replace(/"/g,'')}">\uD83D\uDCF0 ${d.news.slice(0,40)}\u2026</span>`:'';
    tb.insertAdjacentHTML('beforeend',
      `<tr><td><b>${sym}</b></td><td>${d?fmt(d.price,5):DASH}</td>
       <td>${d?fmt(d.atr,6):DASH}</td><td>${d?spreadTxt(d.spread):DASH}</td>
       <td>${regime}</td><td>${d?chips(d.agents):DASH}</td>
       <td>${d?outlook(d):DASH}${d&&d.kronos_summary?'<div class="dim2" style="font-size:10px;max-width:240px;white-space:normal;line-height:1.35;margin-top:3px">'+(d.kronos_summary||'').slice(0,96)+'</div>':''}</td>
       <td>${dir}${d&&d.strength!=null?' <small class="dim2">'+d.strength+'</small>':''}${htf}</td>
       <td>${ai}</td><td class="dim2">${act}${news?'<br>'+news:''}</td></tr>`);
  }

  const t2=$('pos').tBodies[0]; t2.innerHTML='';
  (on?m.positions:[]).forEach(p=>{
    t2.insertAdjacentHTML('beforeend',
      `<tr><td>${p.ticket}</td><td><b class="${p.dir=='BUY'?'pos':'neg'}">${p.dir}</b></td><td>${p.symbol}</td><td>${p.volume}</td><td>${p.open}</td><td>${p.sl||DASH}</td><td>${p.tp||DASH}</td><td class="${cls(p.pl)}"><b>${sign(p.pl)}</b></td></tr>`);
  });
  if(!on||!m.positions.length)
    t2.insertAdjacentHTML('beforeend','<tr><td colspan=8 class="dim2">no open bot positions</td></tr>');

  const t3=$('dec').tBodies[0]; t3.innerHTML='';
  s.decisions.forEach(d=>{
    t3.insertAdjacentHTML('beforeend',
    `<tr><td>${d.when}</td><td><b>${d.symbol}</b></td><td><b>${d.action}</b></td><td>${d.dir}</td><td>${d.volume}</td><td>${d.strength}</td><td>${d.conf}</td><td>${d.llm}</td><td>${d.executed?'\u2705':DASH}</td><td class="dim2">${d.reason}</td></tr>`);
  });
  if(!s.decisions.length)
    t3.insertAdjacentHTML('beforeend','<tr><td colspan=10 class="dim2">no decisions yet \u2014 the journal fills on every closed bar</td></tr>');

  renderPlans(s); renderKronos(s);
  renderTicker(s); renderFunnel(s.funnel); renderGuards(s);
  $('log').textContent=(s.log_tail||'');
  $('haltbanner').style.display=/GUARDIAN LOCKOUT/i.test(s.log_tail||'')?'block':'none';
}

async function startRunner(mode){
  const confirmLive=mode=='live'&&!confirm('Start LIVE trading \u2014 real orders on the connected MT5 account?');
  if(confirmLive) return;
  const r=await (await fetch('/api/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})).json();
  if(!r.ok) alert('Could not start: '+r.error);
  snap();
}
async function stopRunner(){ await fetch('/api/stop',{method:'POST'}); snap(); }
async function resetGuardian(){ await fetch('/api/reset_guardian',{method:'POST'});
  alert('Guardian reset signal sent \u2014 processed in the next runner cycle.'); }

setInterval(()=>{
  syncSecs=Math.max(0,syncSecs-1);
  const a=$('autosync'); if(a) a.innerHTML='\u21BB '+syncSecs+'s';
  const c=$('clock'); if(c) c.textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
},1000);
snap(); setInterval(snap,3000); setInterval(loadChart,15000);
</script>
</body></html>

"""


@app.get("/")
def index() -> Response:
    return Response(PAGE, mimetype="text/html")


def main() -> int:
    ap = argparse.ArgumentParser(description="TradingAgents dashboard")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    url = f"http://localhost:{args.port}"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Dashboard: {url}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
