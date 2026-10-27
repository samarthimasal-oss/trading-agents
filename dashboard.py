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
            "llm_model": cfg["llm"].get("gemini_model"),
            "llm_enabled": cfg["llm"].get("enabled", True),
        },
        "strategies": strategies_list(),
        "live": live_board(),
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
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>TradingAgents Desk</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
 :root{--bg:#0a0e14;--bg2:#0d1320;--card:#101725;--card2:#0c111c;--edge:#1d2939;
   --tx:#e8eef7;--dim:#8395a9;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;
   --blue:#38bdf8;--violet:#a78bfa;--cyan:#22d3ee}
 *{box-sizing:border-box}
 body{margin:0;background:radial-gradient(1200px 500px at 70% -10%, #12203a66, transparent),
   linear-gradient(180deg,var(--bg),var(--bg2));color:var(--tx);
   font:14px/1.5 "Segoe UI",system-ui,sans-serif}
 header{display:flex;align-items:center;gap:12px;padding:12px 20px;flex-wrap:wrap;
   border-bottom:1px solid var(--edge);
   background:linear-gradient(90deg,#0d1526,#101a2e 55%,#0d1526)}
 header h1{font-size:17px;margin:0;font-weight:800;letter-spacing:.6px;display:flex;align-items:center;gap:9px}
 header h1 .logo{width:26px;height:26px;border-radius:8px;display:inline-flex;
   align-items:center;justify-content:center;font-size:15px;
   background:linear-gradient(135deg,#1d4ed8,#38bdf8);box-shadow:0 2px 12px #38bdf855}
 header h1 span{background:linear-gradient(90deg,#38bdf8,#a78bfa);
   -webkit-background-clip:text;background-clip:text;color:transparent}
 .badge{padding:4px 14px;border-radius:20px;font-size:12px;font-weight:800;
   border:1px solid var(--edge);letter-spacing:.8px}
 .b-stop{color:var(--dim)} .b-dry{color:var(--amber);border-color:var(--amber);background:#451a0333}
 .b-live{color:#4ade80;border-color:#22c55e;background:linear-gradient(90deg,#052e16,#0a3d20);
   box-shadow:0 0 16px #22c55e33}
 .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
 .dot.on{background:var(--green);box-shadow:0 0 9px #22c55e99}
 .dot.off{background:var(--red);box-shadow:0 0 9px #ef444499}
 .pill{font-size:12px;color:var(--dim);border:1px solid var(--edge);
   padding:4px 11px;border-radius:8px;background:#0c1422aa}
 button{background:linear-gradient(180deg,#1a2437,#141c2e);color:var(--tx);
   border:1px solid var(--edge);border-radius:9px;padding:8px 16px;cursor:pointer;
   font-size:13px;font-weight:700;transition:all .15s}
 button:hover{border-color:var(--blue);transform:translateY(-1px);
   box-shadow:0 4px 14px #38bdf822}
 button.warn{border-color:#7f1d1d;color:#fca5a5}
 button.go{border-color:#14532d;color:#4ade80}
 .banner{padding:10px 20px;background:repeating-linear-gradient(45deg,#450a0a,#450a0a 12px,#5c1010 12px,#5c1010 24px);
   color:#fecaca;display:none;font-weight:700;border-bottom:1px solid #7f1d1d}
 .grid{display:grid;gap:12px;padding:14px 20px;grid-template-columns:repeat(auto-fit,minmax(225px,1fr))}
 .g12{display:grid;grid-template-columns:2fr 1fr;gap:12px;padding:14px 20px 2px}
 @media (max-width:900px){ .g12{grid-template-columns:1fr} }
 .card{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--edge);
   border-radius:14px;padding:15px 17px;box-shadow:0 8px 24px #0006;position:relative;overflow:hidden}
 .kpi .v{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:.3px}
 .kpi .l{color:var(--dim);font-size:10.5px;letter-spacing:1.2px;text-transform:uppercase;
   display:flex;align-items:center;gap:6px}
 .kpi .ico{font-size:13px;opacity:.8}
 .kpi small{display:block;color:var(--dim);margin-top:3px;font-size:11.5px}
 .k-p{--ic:var(--green)} .k-n{--ic:var(--red)} .k-b{--ic:var(--blue)} .k-v{--ic:var(--violet)}
 .k-a{--ic:var(--amber)} .k-c{--ic:var(--cyan)}
 .kpi::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
   background:linear-gradient(180deg,transparent,var(--ic,#38bdf8),transparent)}
 .pos{color:var(--green)} .neg{color:var(--red)} .dimc{color:var(--dim)}
 table{width:100%;border-collapse:collapse;font-size:12.5px}
 th{color:var(--dim);text-align:left;padding:6px 7px;border-bottom:1px solid var(--edge);
   font-size:10.5px;letter-spacing:1px;text-transform:uppercase}
 td{padding:6px 7px;border-bottom:1px solid #141e30;font-variant-numeric:tabular-nums}
 tbody tr:hover{background:#141d2f66}
 .wide{grid-column:1/-1}
 h3{margin:0 0 10px;font-size:12.5px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);
   display:flex;align-items:center;gap:8px}
 h3 .live-pill{font-size:9.5px;background:#22d3ee14;color:var(--cyan);border:1px solid #22d3ee44;
   padding:1px 8px;border-radius:10px;letter-spacing:.5px}
 pre{margin:0;font:12px/1.45 Consolas,monospace;color:#9fb2c6;max-height:250px;
   overflow:auto;white-space:pre-wrap}
 button.mini{padding:4px 10px;font-size:12px;border-radius:6px}
 .arrow{font-size:14px;font-weight:800}
 .chip{display:inline-block;border:1px solid var(--edge);border-radius:5px;
   padding:1px 6px;margin:1px 2px;font-size:11px;color:var(--dim);cursor:default}
 .chip.u{color:var(--green);border-color:#14532d;background:#052e1633}
 .chip.d{color:var(--red);border-color:#7f1d1d;background:#2a0a0a33}
 .chip.news{color:#fca5a5;border-color:#7f1d1d;background:#450a0a55;cursor:help;white-space:normal}
 .chip.tf{cursor:pointer}
 .chip.tf:hover{border-color:var(--blue)}
 .regime{font-weight:700} .regime.ok{color:var(--green)} .regime.dead{color:var(--red)}
 .dim2{color:var(--dim)}
 select{background:#182030;color:var(--tx);border:1px solid var(--edge);
   border-radius:8px;padding:7px 10px;font-size:13px}
 canvas{width:100%;display:block}
 .strat{display:flex;gap:10px;align-items:baseline;padding:7px 0;
   border-bottom:1px dashed #16202e}
 .strat b{min-width:88px;text-transform:capitalize} .strat .w{color:var(--violet);font-weight:800;
   font-variant-numeric:tabular-nums}
 .strat .d{color:var(--dim);font-size:12px}
 .updated{color:var(--dim);font-size:11px;font-weight:400;text-transform:none;letter-spacing:0}
 .tfbar{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
 .tftab{padding:5px 13px;border-radius:8px;border:1px solid var(--edge);cursor:pointer;
   font-size:12px;font-weight:800;color:var(--dim);background:#0c1424;user-select:none}
 .tftab.on{color:#fff;border-color:var(--blue);
   background:linear-gradient(180deg,#123049,#0f2238);box-shadow:0 0 10px #38bdf833}
 .legend{display:flex;gap:14px;color:var(--dim);font-size:11px;align-items:center;flex-wrap:wrap}
 .legend i{width:14px;height:2.5px;border-radius:2px;display:inline-block;vertical-align:middle;margin-right:5px}
 /* ---- new: live feed ticker ---- */
 .ticker{display:flex;align-items:center;gap:12px;padding:10px 16px;margin:12px 20px}
 .tk-label{font-size:10px;letter-spacing:1.4px;color:var(--cyan);font-weight:800;
   white-space:nowrap;display:flex;align-items:center;gap:7px}
 .tk-dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);animation:blink 2s infinite}
 @keyframes blink{0%,60%{opacity:1}70%,100%{opacity:.25}}
 .tk-scroll{display:flex;gap:8px;overflow-x:auto;scrollbar-width:thin;flex:1;
   white-space:nowrap;padding-bottom:2px}
 .tk-chip{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--edge);
   border-radius:20px;padding:3px 11px;font-size:11.5px;color:var(--dim);
   background:#0c1422;flex:0 0 auto}
 .tk-chip b{color:var(--tx);font-variant-numeric:tabular-nums}
 .tk-chip.fill{border-color:#14532d;color:#86efac}
 .tk-chip.close{border-color:#365314;color:#d9f99d}
 .tk-chip.reject{border-color:#7f1d1d;color:#fca5a5}
 .tk-chip.sl{border-color:#92400e;color:#fcd34d}
 .tk-chip.fresh{animation:pop .8s ease-out}
 @keyframes pop{0%{transform:scale(.85);box-shadow:0 0 0 #22c55e00}40%{transform:scale(1.08);box-shadow:0 0 18px #22c55e55}100%{transform:scale(1)}}
 /* ---- new: gauges ---- */
 .gauge{margin:7px 0 3px}
 .gauge .gl{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:3px}
 .gauge .bar{height:7px;border-radius:5px;background:#131d2e;overflow:hidden}
 .gauge .fillb{height:100%;border-radius:5px;transition:width .6s;background:linear-gradient(90deg,#22c55e,#a3e635)}
 .gauge .fillb.warn{background:linear-gradient(90deg,#f59e0b,#f97316)}
 .gauge .fillb.hot{background:linear-gradient(90deg,#ef4444,#b91c1c)}
 .kpi-big .v{font-size:31px;text-shadow:0 0 22px #22c55e22}
 /* ---- new: committee card ---- */
 .funnel{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;font-size:12.5px}
 .funnel .fi{display:flex;justify-content:space-between;align-items:center;
   border-bottom:1px dashed #16202e;padding:4px 0}
 .funnel .fi b{font-size:14px;font-variant-numeric:tabular-nums}
 .funnel .fl{display:flex;align-items:center;gap:6px;color:var(--dim)}
 /* ---- new: table wrap for mobile ---- */
 .tablewrap{overflow-x:auto}
 .tablewrap table{min-width:640px}
 @media (max-width:620px){
   .kpi .v{font-size:21px} .kpi-big .v{font-size:24px}
   header{padding:10px 12px;gap:8px} .grid,.g12{padding:10px 12px}
   .ticker{margin:10px 12px}
 }
 @keyframes kpiFlash{0%{box-shadow:0 0 0 #22c55e00}30%{box-shadow:0 0 26px #22c55e66}100%{box-shadow:0 8px 24px #0006}}
 .flashcard{animation:kpiFlash 1.4s ease-out}
</style></head>
<body>
<header>
 <h1><span class="logo">&#9672;</span>TradingAgents <span>DESK</span></h1>
 <span id="mode" class="badge b-stop">STOPPED</span>
 <span><span class="dot off" id="mt5dot"></span><b id="mt5label">connecting&#8230;</b></span>
 <span class="pill" id="cfgline"></span>
 <span class="pill" id="autosync">&#8635; &hellip;</span>
 <span class="pill" id="clock">&ndash;:&ndash;&ndash;</span>
 <span style="flex:1"></span>
 <button class="go" onclick="startRunner('dry')">&#9654; DRY-RUN</button>
 <button onclick="startRunner('live')">&#9654; LIVE</button>
 <button class="warn" onclick="stopRunner()">&#9632; STOP</button>
 <button class="mini" onclick="resetGuardian()">&#8635; guardian</button>
</header>
<div id="haltbanner" class="banner">GUARDIAN LOCKOUT &#8212; trading halted (risk limit hit). Reset guardian button above.</div>

<div class="card ticker">
 <span class="tk-label"><span class="tk-dot"></span>LIVE FEED</span>
 <div class="tk-scroll" id="ticker"><span class="tk-chip">quiet&#8230; waiting for broker events</span></div>
</div>

<div class="grid">
 <div class="card kpi k-b"><div class="l"><span class="ico">&#128176;</span>Equity</div><div class="v" id="equity">&#8212;</div><small id="equitysub"></small></div>
 <div class="card kpi k-v"><div class="l"><span class="ico">&#127974;</span>Balance</div><div class="v" id="balance">&#8212;</div></div>
 <div class="card kpi k-n kpi-big"><div class="l"><span class="ico">&#128197;</span>Today's bot P/L</div><div class="v" id="daily">&#8212;</div><small id="flush"></small></div>
 <div class="card kpi k-p kpi-big"><div class="l"><span class="ico">&#128200;</span>Floating P/L</div><div class="v" id="floating">&#8212;</div></div>
 <div class="card kpi k-a"><div class="l"><span class="ico">&#128275;</span>Free margin (level)</div><div class="v" id="freemargin">&#8212;</div><small id="mlsub"></small></div>
 <div class="card kpi k-c"><div class="l"><span class="ico">&#129504;</span>Decisions / Filled</div><div class="v" id="counts">&#8212;</div><small id="llmstate"></small></div>
 <div class="card"><div class="l" style="color:var(--dim);font-size:10.5px;letter-spacing:1.2px;text-transform:uppercase">
   <span class="ico" style="font-size:13px">&#128737;</span>Risk guard</div>
  <div class="gauge"><div class="gl"><span>Day-loss guard used</span><b id="gdaytxt">0%</b></div>
    <div class="bar"><div class="fillb" id="gday" style="width:0%"></div></div></div>
  <div class="gauge"><div class="gl"><span>Drawdown from peak</span><b id="gddtxt">0%</b></div>
    <div class="bar"><div class="fillb" id="gdd" style="width:0%"></div></div></div>
  <small id="gpeaks" class="dimc" style="display:block;margin-top:4px"></small>
 </div>
</div>

<div class="g12">
 <div class="card" id="chartcard">
   <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:10px;flex-wrap:wrap">
     <h3 style="margin:0">Market analysis <span class="live-pill">KRONOS FORECAST</span></h3>
     <select id="symsel" onchange="loadChart()"></select>
   </div>
   <div class="tfbar" id="tfsel"></div>
   <div class="legend" style="margin-bottom:6px">
     <span><i style="background:#38bdf8"></i>EMA fast</span>
     <span><i style="background:#a78bfa"></i>EMA slow</span>
     <span><i style="background:repeating-linear-gradient(90deg,#f59e0b 0 4px,transparent 4px 7px)"></i>Kronos forecast</span>
     <span id="chartmeta" style="margin-left:auto"></span>
   </div>
   <canvas id="chart" height="320"></canvas>
 </div>
 <div>
  <div class="card">
    <h3>Strategy desk</h3>
    <div id="strats"></div>
  </div>
  <div class="card" style="margin-top:12px">
    <h3>Equity curve <span class="live-pill" id="eqmeta"></span></h3>
    <canvas id="eqspark" height="130"></canvas>
  </div>
  <div class="card" style="margin-top:12px">
    <h3>Today's committee</h3>
    <div class="funnel" id="funnel">
      <div class="fi"><span class="fl">&#9989; Filled</span><b class="pos" id="f_filled">&#8212;</b></div>
      <div class="fi"><span class="fl">&#128683; LLM veto</span><b class="neg" id="f_vetoed">&#8212;</b></div>
      <div class="fi"><span class="fl">&#128679; Risk blocks</span><b id="f_blocked">&#8212;</b></div>
      <div class="fi"><span class="fl">&#128164; Flat (no trade)</span><b class="dimc" id="f_flat">&#8212;</b></div>
    </div>
    <small class="dimc" style="display:block;margin-top:6px">fills include rate-limit pass-throughs; blocks = guard gates (positions, spread, regime, news)</small>
  </div>
 </div>
</div>

<div class="grid">
 <div class="card wide"><h3>Signal board <small class="updated" id="boardage"></small></h3>
   <div class="tablewrap"><table id="board"><thead><tr>
     <th>Symbol</th><th>Last</th><th>ATR</th><th>Spread</th><th>Regime</th>
     <th>Agents</th><th>Kronos outlook</th><th>Consensus</th><th>AI verdict</th><th>Status</th>
   </tr></thead><tbody></tbody></table></div></div>

 <div class="card wide"><h3>Open bot positions</h3>
   <div class="tablewrap"><table id="pos"><thead><tr>
     <th>ID</th><th>Side</th><th>Symbol</th><th>Volume</th><th>Entry</th>
     <th>SL</th><th>TP</th><th>P/L</th></tr></thead><tbody></tbody></table></div></div>

 <div class="card wide"><h3>Decision journal <small class="updated" id="jstat"></small></h3>
   <div class="tablewrap"><table id="dec"><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Dir</th>
     <th>Vol</th><th>Strength</th><th>Conf</th><th>LLM</th><th>Exec</th><th>Reason</th></tr></thead><tbody></tbody></table></div></div>

 <div class="card wide"><h3>Live log</h3> <pre id="log">&#8230;</pre></div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = (v,d=2) => v==null||isNaN(v) ? '\u2014' : (+v).toLocaleString(undefined,{maximumFractionDigits:d});
const cls = v => v>=0 ? 'pos':'neg';
const sign = v => v==null||isNaN(v) ? '\u2014' : (v>0?'+':'')+(+v).toFixed(2);
let chartData = null, chartSymbol = '', chartTF = 'M15', KRONOS_TFS = ['M15'];
window.DEAD_RATIO = 0.62;
const DASH = '\u2014';

function buildTfTabs(){
  const bar = $('tfsel'); bar.innerHTML='';
  (KRONOS_TFS.length?KRONOS_TFS:['M15']).forEach(tf=>{
    const d = document.createElement('div');
    d.className = 'tftab' + (tf===chartTF?' on':'');
    d.textContent = tf;
    d.onclick = ()=>{ chartTF = tf; buildTfTabs(); loadChart(); };
    bar.appendChild(d);
  });
}

function drawCandles(){
  const cv = $('chart'); const c = cv.getContext('2d');
  cv.width = cv.clientWidth;
  c.clearRect(0,0,cv.width,cv.height);
  c.fillStyle='#7d8b9e'; c.font='12px sans-serif';
  if(!chartData || !chartData.candles || chartData.candles.length<3){
    c.fillText('waiting for MT5 data ('+'open + log into the terminal)'+DASH, 10, 30); return; }
  const K = chartData.candles, d=chartData.digits, V = chartData.volume||[];
  const F = chartData.forecast || [];
  const n=K.length, m=F.length;
  const hi0 = Math.max(...K.map(k=>k[2])), lo0 = Math.min(...K.map(k=>k[3]));
  const hi = Math.max(hi0, ...F.map(k=>k[2])), lo = Math.min(lo0, ...F.map(k=>k[3]));
  const pad = (hi-lo)*0.08 || 1e-6; const top = hi+pad, bot = lo-pad;
  const H = cv.height, W = cv.width, L = 8, R = 64, T = 10, B = 24;
  const Y = v => T + (top - v)/(top-bot)*(H-T-B);
  const cw = (W-L-R)/(n+m);
  // grid + price axis
  c.strokeStyle='#17222f'; c.fillStyle='#7d8b9e'; c.font='10px monospace';
  for(let i=0;i<=4;i++){ const v = bot + (top-bot)*i/4, y = Y(v);
    c.beginPath(); c.moveTo(L,y); c.lineTo(W-R,y); c.stroke();
    c.fillText(v.toFixed(d), W-58, y+3); }
  // time axis markers
  const intraday = chartTF.charAt(0)==='M' || chartTF==='H1';
  const tstep = Math.max(1, Math.round((n+m)/5));
  for(let i=0;i<n;i+=tstep){
    c.fillStyle='#5b6b80';
    const dt = new Date(K[i][0]*1000);
    const lbl = intraday ? dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})
                         : dt.toLocaleDateString([], {month:'2-digit',day:'2-digit'});
    c.fillText(lbl, L+cw*i, H-8);
  }
  // volume band (bottom 15% overlay)
  if(V.length){
    const vmax = Math.max(...V), bh = (H-T-B)*0.15;
    const bw = Math.max(1.5, cw*0.55);
    V.forEach((v,i)=>{
      const x = L + cw*i + cw*0.5;
      const up = K[i] && K[i][4]>=K[i][1];
      c.fillStyle = up ? '#22c55e30' : '#ef444430';
      const hb = Math.max(1, v/vmax*bh);
      c.fillRect(x-bw/2, H-B-hb, bw, hb);
    });
  }
  // candles
  K.forEach((k,i)=>{
    const x = L + cw*i + cw*0.5, up = k[4] >= k[1];
    c.strokeStyle = up ? '#22c55e' : '#ef4444'; c.fillStyle = up ? '#0e3b22':'#3b1212';
    const bw = Math.max(1.5, cw*0.55);
    const yO = Y(k[1]), yC = Y(k[4]);
    const yT = Math.min(yO,yC), hgt = Math.max(Math.abs(yC-yO),1);
    c.beginPath(); c.moveTo(x, Y(k[2])); c.lineTo(x, Y(k[3])); c.stroke();
    c.fillRect(x-bw/2, yT, bw, hgt); c.strokeRect(x-bw/2, yT, bw, hgt);
  });
  // Kronos forecast candles (dashed amber outline, starting after last real bar)
  if(m){
    const sepX = L + cw*n;
    c.setLineDash([4,4]); c.strokeStyle='#f59e0b77';
    c.beginPath(); c.moveTo(sepX, T); c.lineTo(sepX, H-B); c.stroke(); c.setLineDash([]);
    c.fillStyle='#f59e0b'; c.font='bold 10px sans-serif';
    c.fillText('Kronos forecast \u2192' + (chartData.forecast_ood?'  \u26A0 OOD':''), sepX+4, T+11);
    F.forEach((k,j)=>{
      const x = L + cw*(n+j) + cw*0.5;
      c.strokeStyle='#f59e0b'; c.setLineDash([3,2]);
      c.beginPath(); c.moveTo(x, Y(k[2])); c.lineTo(x, Y(k[3])); c.stroke();
      c.setLineDash([]);
      const yO=Y(k[1]), yC=Y(k[4]);
      const yT=Math.min(yO,yC), hgt=Math.max(Math.abs(yC-yO),1);
      c.strokeStyle = k[4]>=k[1] ? '#fbbf24' : '#b45309';
      c.strokeRect(x-Math.max(1.5,cw*0.55)/2, yT, Math.max(1.5,cw*0.55), hgt);
    });
  }
  // EMA overlays
  const line = (arr,col)=>{ c.strokeStyle=col; c.lineWidth=1.4; c.beginPath();
    arr.forEach((v,i)=>{ const x=L+cw*i+cw*0.5, y=Y(v); i?c.lineTo(x,y):c.moveTo(x,y); });
    c.stroke(); c.lineWidth=1; };
  if(chartData.ema_fast) line(chartData.ema_fast, '#38bdf8');
  if(chartData.ema_slow) line(chartData.ema_slow, '#a78bfa');
  // last price tag
  const last = K[n-1], lx = L+cw*n, ly = Y(last[4]);
  c.setLineDash([3,3]); c.strokeStyle='#94a3b8';
  c.beginPath(); c.moveTo(L,ly); c.lineTo(W-58,ly); c.stroke(); c.setLineDash([]);
  c.fillStyle = last[4]>=last[1] ? '#065f46' : '#7f1d1d';
  c.fillRect(lx+2, ly-9, 56, 18); c.fillStyle='#fff'; c.font='bold 11px monospace';
  c.fillText(last[4].toFixed(d), lx+8, ly+4);
  // watermark
  c.fillStyle='#334155'; c.font='bold 13px sans-serif';
  c.fillText(chartData.symbol + '  ' + chartData.timeframe, L+4, T+13);
}

async function loadChart(){
  const sel = $('symsel'); const sym = sel.value || chartSymbol;
  if(!sym) return; chartSymbol = sym;
  try{
    const r = await fetch('/api/chart?symbol='+encodeURIComponent(sym)+'&tf='+chartTF+'&bars=110');
    const data = await r.json();
    if(data.error){ chartData=null; }
    else chartData = data;
    $('chartmeta').textContent = data.error ? ('chart unavailable: '+data.error) :
       ('RSI ' + (chartData.rsi?chartData.rsi[chartData.rsi.length-1]:DASH) + '  \u00B7  ATR ' + chartData.atr +
        (chartData.forecast_summary ? '  \u00B7  \uD83E\uDD16 Kronos('+chartTF+'): ' + chartData.forecast_summary
         : '  \u00B7  Kronos('+chartTF+'): preparing\u2026'));
  }catch(e){ $('chartmeta').textContent = 'chart fetch failed'; }
  drawCandles();
}

function chips(votes){
  const map = {trend:1, momentum:1, reversion:1, breakout:1, pullback:1, regime:1, kronos:1};
  return Object.entries(votes||{}).filter(([k])=>map[k]).map(([k,v]) =>
    `<span class="chip ${v>0?'u':v<0?'d':''}">${k[0]}${k==='kronos'?'\u2726':''}${v>0?'\u2191':v<0?'\u2193':'\u00B7'}</span>`
  ).join('');
}

function outlook(d){
  if(!d || !d.forecasts) return DASH;
  return ['M15','H1','H4','D1'].map(tf=>{
    const f=(d.forecasts||{})[tf];
    if(!f) return '<span class="chip">'+tf+' \u00B7</span>';
    const a=f.dir>0?'\u25B2':f.dir<0?'\u25BC':'\u00B7';
    const mark=f.ood?'\u26A0':'';
    return `<span class="chip tf ${f.dir>0?'u':f.dir<0?'d':''}" title="${(f.summary||'').replace(/"/g,'')}"
      onclick="chartTF='${tf}';buildTfTabs();loadChart()">${tf}${mark} ${a}</span>`;
  }).join('');
}

function drawEquity(curve){
  const cv = $('eqspark'); if(!cv) return; const c = cv.getContext('2d');
  cv.width = cv.clientWidth;
  c.clearRect(0,0,cv.width,cv.height);
  if(!curve || curve.length<2){ c.fillStyle='#7d8b9e'; c.font='12px sans-serif';
    c.fillText('the curve draws once the bot logs decisions\u2026', 10, 26); return; }
  const pts = curve.map(p=>[+p[0], +p[1]]);
  const vals = pts.map(p=>p[1]);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const pad=(mx-mn)*0.12||1, top=mx+pad, bot=mn-pad;
  const H=cv.height, W=cv.width, T=6, Bm=16, L=4, R=4;
  const X=i=>L+i/(pts.length-1)*(W-L-R);
  const Y=v=>T+(top-v)/(top-bot)*(H-T-Bm);
  c.strokeStyle='#17222f'; c.fillStyle='#60708a'; c.font='9px monospace';
  for(let i=0;i<=2;i++){ const v=bot+(top-bot)*i/2, y=Y(v);
    c.beginPath(); c.moveTo(L,y); c.lineTo(W-R,y); c.stroke(); }
  const g=c.createLinearGradient(0,T,0,H); g.addColorStop(0,'#22c55e55'); g.addColorStop(1,'#22c55e00');
  c.beginPath(); c.moveTo(X(0),Y(vals[0]));
  pts.forEach((p,i)=>c.lineTo(X(i),Y(p[1])));
  c.lineTo(X(pts.length-1),H-Bm); c.lineTo(X(0),H-Bm); c.closePath(); c.fillStyle=g; c.fill();
  c.beginPath(); c.strokeStyle='#4ade80'; c.lineWidth=1.6;
  pts.forEach((p,i)=> i?c.lineTo(X(i),Y(p[1])):c.moveTo(X(i),Y(p[1])));
  c.stroke();
  c.fillStyle='#94a3b8'; c.font='10px monospace';
  c.fillText('min '+fmt(mn), L+2, H-3);
  c.fillText('now '+fmt(vals[vals.length-1]), W-70, Y(vals[vals.length-1])-4);
}

// ---------- live feed / funnel / guards helpers ----------
let lastEventTs = 0, syncSecs = 0;

function tickChip(e, fresh){
  const t = new Date(e.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const k = e.kind||'', d=(e.detail||'').trim();
  let cls='tk-chip', icon='\u2022', label='';
  if(k==='ORDER_FILLED'){cls+=' fill'; icon='\u26A1'; label='FILL';}
  else if(k==='ORDER_CLOSED'){cls+=' close'; icon='\uD83C\uDFC1'; label='CLOSE';}
  else if(k==='ORDER_REJECTED'){cls+=' reject'; icon='\uD83D\uDEAB'; label='REJECT';}
  else if(k==='SL_MODIFIED'){cls+=' sl'; icon='\uD83D\uDEE1'; label='SL \u2192';}
  return `<span class="${cls}${fresh?' fresh':''}">${icon} ${label} ${e.symbol?e.symbol+' ':''}<b>${d}</b><small class="dimc">\u00B7${t}</small></span>`;
}

function renderTicker(s){
  const el = $('ticker'); if(!el) return;
  const evs = s.events || [];
  el.innerHTML = evs.length
    ? evs.slice().reverse().map(e => tickChip(e, lastEventTs>0 && e.ts > lastEventTs)).join('')
    : '<span class="tk-chip">quiet\u2026 waiting for broker events</span>';
  const newest = evs.length ? evs[evs.length-1].ts : 0;
  if(lastEventTs && newest > lastEventTs){
    el.closest('.card').classList.add('flashcard');
    setTimeout(()=>el.closest('.card').classList.remove('flashcard'), 1500);
  }
  lastEventTs = Math.max(lastEventTs, newest);
}

function renderFunnel(f){
  if(!f) return;
  const map = {filled:'f_filled', vetoed:'f_vetoed', blocked:'f_blocked', flat:'f_flat'};
  Object.entries(map).forEach(([k,id])=>{ const n=$(id); if(n) n.textContent = (f[k]||0); });
}

function renderGuards(s){
  const eq = (s.mt5 && s.mt5.equity) || 0;
  const daily = (s.mt5 && s.mt5.daily_pnl) || 0;
  const limitPct = (s.trading_cfg && s.trading_cfg.daily_loss_limit) || 3;
  const ddLimit = (s.trading_cfg && s.trading_cfg.max_dd_limit) || 15;
  const used = eq>0 ? Math.max(0, -daily)/eq/(limitPct/100)*100 : 0;
  const peak = s.peak || eq;
  const dd = peak>0 ? Math.max(0, (peak-eq)/peak*100) : 0;
  const set = (id, pc)=>{ const b=$(id); if(!b) return;
    b.style.width = Math.max(0, Math.min(100, pc))+'%';
    b.className = 'fillb' + (pc>85?' hot':pc>50?' warn':''); };
  set('gday', used); $('gdaytxt').textContent = used.toFixed(0)+'% of -'+limitPct+'%';
  set('gdd', dd/ddLimit*100); $('gddtxt').textContent = dd.toFixed(2)+'% / '+ddLimit+'%';
  $('gpeaks').textContent = 'peak equity '+fmt(peak)+' \u00B7 day guard -'+limitPct+'% \u00B7 dd max -'+ddLimit+'%';
}

async function snap(){
  let s;
  try{ s = await (await fetch('/api/snapshot')).json(); }
  catch(e){ const a=$('autosync'); if(a) a.innerHTML='\u26A0 retry'; return; }
  syncSecs = 3;
  KRONOS_TFS = (s.trading_cfg.kronos_tfs && s.trading_cfg.kronos_tfs.length)
    ? s.trading_cfg.kronos_tfs : ['M15'];
  if(!$('tfsel').children.length) buildTfTabs();
  window.DEAD_RATIO = s.trading_cfg.dead_ratio || 0.62;
  const mode = s.runner.mode.toUpperCase();
  $('mode').textContent = mode;
  $('mode').className = 'badge ' + (mode=='STOPPED'?'b-stop':mode=='DRY-RUN'?'b-dry':'b-live');
  const m = s.mt5, on = !!m.connected;
  $('mt5dot').className = 'dot ' + (on?'on':'off');
  $('mt5label').textContent = on ? ('\u2713 MT5 ' + m.login + ' \u00B7 ' + (m.positions.length? m.positions.length+' pos':'flat')) : '\u2717 MT5 ' + (m.error||'disconnected');
  $('equity').textContent = on?fmt(m.equity):DASH;
  $('equitysub').textContent = on?('balance '+fmt(m.balance)):'';
  $('balance').textContent = on?fmt(m.balance):DASH;
  $('daily').textContent = on?sign(m.daily_pnl):DASH; $('daily').className='v '+cls(m.daily_pnl||0);
  $('floating').textContent = on?sign(m.floating):DASH; $('floating').className='v '+cls(m.floating||0);
  $('freemargin').textContent = on?fmt(m.free_margin):DASH;
  $('mlsub').textContent = on?('margin level '+m.margin_level+'%'):'';
  const lots = on ? (m.positions.reduce((a,p)=>a+(+p.volume||0),0)) : 0;
  $('flush').textContent = on ? (m.positions.length+' pos \u00B7 '+lots.toFixed(2)+' lots') : '';
  $('counts').textContent = s.stats.decisions+' / '+s.stats.filled_orders;
  $('llmstate').textContent = s.trading_cfg.llm_enabled ? ('AI: '+s.trading_cfg.llm_model) : 'AI: off';
  $('jstat').textContent = s.stats.decisions+' logged, '+s.stats.filled_orders+' filled';
  drawEquity(s.equity);
  const t = s.trading_cfg;
  $('cfgline').textContent = (t.symbols||[]).join(' \u00B7 ')+' '+t.timeframe+' \u00B7 risk '+t.risk_pct+'%';
  $('eqmeta').textContent = ((s.equity||[]).length) + ' pts\u00B7'+t.timeframe;

  // symbol dropdown
  const sel = $('symsel');
  const names = t.symbols || [];
  if(sel.options.length !== names.length || sel.value===''){
    sel.innerHTML = names.map(n=>'<option>'+n+'</option>').join('');
    if(!sel.value) sel.value = chartSymbol || names[0];
  }

  // strategy desk
  $('strats').innerHTML = (s.strategies||[]).map(x =>
    `<div class="strat"><b>${x.name}</b><span class="w">w ${x.weight}</span>
     <span class="d">${x.how}</span></div>`).join('');

  // signal board
  const tb = $('board').tBodies[0]; tb.innerHTML='';
  const board = s.live && s.live.symbols ? s.live.symbols : {};
  let age = s.live ? s.live.age : null;
  $('boardage').textContent = (age==null) ? '(idle)' : (age<60 ? age+'s ago' : Math.round(age/60)+'m ago');
  for(const sym of (t.symbols||[])){
    const d = board[sym];
    const dir = d ? (d.dir>0?'<span class="arrow pos">\u25B2 LONG</span>':d.dir<0?'<span class="arrow neg">\u25BC SHORT</span>':'\u00B7 flat') : DASH;
    const regime = d && d.regime!=null ? `<span class="regime ${d.regime>window.DEAD_RATIO?'ok':'dead'}">${d.regime}</span>` : DASH;
    const ai = d ? (d.llm ? (d.llm+' <span class="chip">'+(d.llm_comment||'').slice(0,36)+'</span>') : 'no consensus') : DASH;
    const act = d ? (d.action||d.why||'') : '';
    const news = d && d.news ? `<span class="chip news" title="${d.news.replace(/"/g,'')}">\uD83D\uDCF0 ${d.news.slice(0,44)}\u2026</span>` : '';
    tb.insertAdjacentHTML('beforeend',
      `<tr><td><b>${sym}</b></td><td>${d?fmt(d.price,5):DASH}</td>
       <td>${d?fmt(d.atr,6):DASH}</td><td>${d?fmt(d.spread,1):DASH}</td>
       <td>${regime}</td><td>${d?chips(d.agents):DASH}</td>
       <td>${d?outlook(d):DASH}${d&&d.kronos_summary?'<div class="dim2" style="font-size:11px;max-width:230px;white-space:normal">'+(d.kronos_summary||'').slice(0,90)+'</div>':''}</td>
       <td>${dir}${d&&d.strength!=null?' <small class="dim2">'+d.strength+'</small>':''}</td>
       <td>${ai}</td><td class="dim2">${act}${news?'<br>'+news:''}</td></tr>`);
  }

  // positions
  const t2 = $('pos').tBodies[0]; t2.innerHTML='';
  (on? m.positions : []).forEach(p=>{
    t2.insertAdjacentHTML('beforeend',
      `<tr><td>${p.ticket}</td><td><b class="${p.dir=='BUY'?'pos':'neg'}">${p.dir}</b></td><td>${p.symbol}</td><td>${p.volume}</td><td>${p.open}</td><td>${p.sl||DASH}</td><td>${p.tp||DASH}</td><td class="${cls(p.pl)}"><b>${sign(p.pl)}</b></td></tr>`);
  });
  if(!on || !m.positions.length)
    t2.insertAdjacentHTML('beforeend','<tr><td colspan=8 class="dim2">no open bot positions</td></tr>');

  // decisions
  const t3 = $('dec').tBodies[0]; t3.innerHTML='';
  s.decisions.forEach(d=>{
    t3.insertAdjacentHTML('beforeend',
    `<tr><td>${d.when}</td><td><b>${d.symbol}</b></td><td><b>${d.action}</b></td><td>${d.dir}</td><td>${d.volume}</td><td>${d.strength}</td><td>${d.conf}</td><td>${d.llm}</td><td>${d.executed?'\u2705':'\u2014'}</td><td class="dim2">${d.reason}</td></tr>`);
  });
  if(!s.decisions.length)
    t3.insertAdjacentHTML('beforeend','<tr><td colspan=10 class="dim2">no decisions yet \u2014 the journal fills on every closed bar</td></tr>');

  renderTicker(s); renderFunnel(s.funnel); renderGuards(s);
  $('log').textContent = (s.log_tail||'');
  $('haltbanner').style.display = /GUARDIAN LOCKOUT/i.test(s.log_tail||'') ? 'block':'none';
}

async function startRunner(mode){
  const confirmLive = mode=='live' && !confirm('Start LIVE trading \u2014 real orders on the connected MT5 account?');
  if(confirmLive) return;
  const r = await (await fetch('/api/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})).json();
  if(!r.ok) alert('Could not start: ' + r.error);
  snap();
}
async function stopRunner(){ await fetch('/api/stop',{method:'POST'}); snap(); }
async function resetGuardian(){ await fetch('/api/reset_guardian',{method:'POST'});
  alert('Guardian reset signal sent \u2014 processed in the next runner cycle.'); }

// 1-second heartbeat: sync countdown + wall clock
setInterval(()=>{
  syncSecs = Math.max(0, syncSecs-1);
  const a = $('autosync'); if(a) a.innerHTML = '\u21BB '+syncSecs+'s';
  const c = $('clock'); if(c) c.textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}, 1000);
snap(); setInterval(snap, 3000); setInterval(loadChart, 15000); loadChart();
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
