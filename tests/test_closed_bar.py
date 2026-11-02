"""Closed-bar discipline: risk inputs must never come from the forming candle."""
from __future__ import annotations

import json
import os

import pandas as pd

from agents import quant
from agents.risk_agent import RiskAgent
from core.models import (AccountSnapshot, Consensus, LlmReview, MarketContext,
                         Signal)
from core.mt5_client import MT5Client


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg() -> dict:
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_apply_closed_indicators_uses_last_closed_row():
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15")
    ctx.atr = 999.0
    ctx.rsi = 1.0
    closed = pd.DataFrame({
        "ema_fast": [10.0, 11.0],
        "ema_slow": [9.0, 9.5],
        "rsi": [55.0, 60.0],
        "atr": [2.0, 2.5],
    })
    quant.apply_closed_indicators(ctx, closed)
    assert ctx.ema_fast == 11.0
    assert ctx.ema_slow == 9.5
    assert ctx.rsi == 60.0
    assert ctx.atr == 2.5


def test_build_plan_uses_explicit_closed_atr(monkeypatch):
    cfg = _cfg()
    info = {
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_contract_size": 100.0, "trade_tick_value": 1.0,
        "trade_tick_size": 0.01, "point": 0.01, "digits": 2,
        "stops_level": 10, "freezing_level": 0,
    }
    monkeypatch.setattr(MT5Client, "symbol_settings", staticmethod(lambda _info: info))

    class FakeClient:
        def prepare(self, symbol):
            return object()

        def tick(self, symbol):
            class T:
                bid = 2000.0
                ask = 2000.5
            return T()

    ctx = MarketContext(symbol="XAUUSD", timeframe="M15",
                        bid=2000.0, ask=2000.5, point=0.01, digits=2)
    ctx.atr = 999.0                      # deliberately wrong forming-bar value
    agent = RiskAgent(cfg, FakeClient())
    consensus = Consensus(direction=1, strength=0.8, confidence=0.8,
                          signals=[Signal("trend", 1, 1.0, 0.8, "up")])
    review = LlmReview(approved=True, action="approve", used_llm=False)
    account = AccountSnapshot(equity=100000.0, balance=100000.0)

    plan = agent.build_plan(ctx, None, cfg["agents"], consensus, review,
                            account, atr=2.0)
    assert plan.atr == 2.0
    assert abs((plan.entry_hint - plan.sl) - 2.0 * cfg["agents"]["atr_sl_mult"]) < 1e-6
    assert abs((plan.tp - plan.entry_hint) - 2.0 * cfg["agents"]["atr_tp_mult"]) < 1e-6
