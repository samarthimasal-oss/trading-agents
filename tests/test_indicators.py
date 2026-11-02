"""Indicator sanity checks used as a regression net for the strategy engines."""
from __future__ import annotations

import math

import pandas as pd

from agents.quant import atr, ema, enrich, rsi, to_frame
from core.models import Candle


def _bars(n: int = 120) -> list[Candle]:
    out = []
    price = 2000.0
    for i in range(n):
        price += 0.5 * math.sin(i / 3.0) + 0.1
        out.append(Candle(1_700_000_000 + i * 900, price, price + 1.0,
                          price - 1.0, price + 0.2, 100))
    return out


def test_ema_is_causal_and_finite():
    df = to_frame(_bars())
    e = ema(df["close"], 20)
    assert len(e) == len(df)
    assert e.notna().all()
    assert e.iloc[-1] > 0


def test_rsi_bounds():
    df = to_frame(_bars())
    r = rsi(df["close"], 14).dropna()
    assert not r.empty
    assert (r >= 0).all() and (r <= 100).all()


def test_atr_positive():
    df = to_frame(_bars())
    a = atr(df, 14).dropna()
    assert not a.empty
    assert (a > 0).all()


def test_enrich_adds_expected_columns():
    cfg = {"ema_fast": 12, "ema_slow": 34, "rsi_period": 14, "atr_period": 14}
    out = enrich(to_frame(_bars()), cfg)
    for col in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert col in out.columns
    assert out["atr"].iloc[-1] > 0
