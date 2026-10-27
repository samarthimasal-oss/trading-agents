import json
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import quant
from agents.quant import enrich, to_frame
from agents.llm_agent import load_api_key, LlmAgent
from core.models import Candle, MarketContext, AccountSnapshot, Consensus, Signal
from backtest import run as bt_run

HERE = os.path.dirname(os.path.abspath(__file__))

random.seed(7)
price = 1.1000
bars = []
for i in range(600):
    drift = 0.00008 if i % 40 > 20 else -0.00006
    price += drift + random.uniform(-0.0007, 0.0007)
    o = price
    h = o + abs(random.uniform(0, 0.0009))
    l = o - abs(random.uniform(0, 0.0009))
    c = o + random.uniform(-0.0005, 0.0005)
    bars.append(Candle(1700000000 + i * 900, o, max(h, o, c), min(l, o, c), c, 1000))

df = to_frame(bars)
cfg = json.load(open(os.path.join(HERE, "config.json")))
enc = enrich(df, cfg["agents"])
q = quant.run_quant_agents(enc, cfg["agents"])
print("quant consensus OK -> dir=%s strength=%.3f conf=%.3f agents=%s"
      % (q.direction, q.strength, q.confidence, [(s.name, s.direction) for s in q.signals]))

res = bt_run(df, cfg)
print("backtest OK -> trades=%s win_rate=%s total_R=%s pf=%s eq_len=%s"
      % (res.trades, res.win_rate, res.total_R, res.profit_factor, len(res.equity_curve_R)))

key = load_api_key(cfg["llm"])
print("gemini key ->", "FOUND" if key else "missing")

# ---- risk sizing smoke test (pure math, no MT5 needed) ----
info = {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001, "point": 0.00001,
        "digits": 5, "stops_level": 10, "freezing_level": 0}
from agents.risk_agent import RiskAgent
from core.models import MarketContext, AccountSnapshot, Consensus, LlmReview

class FakeClient:
    def prepare(self, s):
        return info

ra = RiskAgent(cfg, FakeClient())
vol = ra.size_position(MarketContext(symbol="EURUSD", timeframe="M15"),
                       info, equity=10000.0, atr_val=0.0012)
print("risk sizing OK -> volume for 1% risk on 10k with ATR .0012 =", vol)

# ---- LLM committee live call (validates graceful fallback too) ----
try:
    from agents.llm_agent import LlmAgent

    cons = Consensus(direction=1, strength=0.6, confidence=0.7,
                     signals=[Signal("trend", 1, 1.0, 0.8, "EMA crossover up"),
                              Signal("momentum", 1, 1.0, 0.6, "RSI 58 rising")])
    ctx = MarketContext(symbol="EURUSD", timeframe="M15", rsi=58.0, atr=0.0012,
                        bid=1.1050, ask=1.10506, spread_points=0.6, digits=5,
                        point=0.00001)
    acc = AccountSnapshot(balance=10000, equity=10000)
    agent = LlmAgent(cfg["llm"])
    agent.min_interval = 0
    review = agent.review(ctx, cons, acc, [],
                          {"kind": "market_order", "direction": "long",
                           "atr": 0.0012, "price": 1.1050})
    print("LLM review OK -> used=%s action=%s mult=%.2f comment=%r err=%r"
          % (review.used_llm, review.action, review.size_multiplier,
             review.comment[:80], review.error[:120]))
except Exception as exc:
    print("LLM review FAILED (graceful fallback expected) ->", str(exc)[:200])
