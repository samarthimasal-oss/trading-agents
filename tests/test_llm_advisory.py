"""Gemini approval semantics: advisory by default, mandatory only when asked.

The critical regression: with ``require_llm_approval=false`` a Gemini veto
used to zero the position size and silently skip the trade.  It must instead
stay advisory (displayed/journaled, never blocking).  Hard risk gates are not
touched by these tests.
"""
from types import SimpleNamespace

import pytest

from agents.risk_agent import RiskAgent
from core.models import AccountSnapshot, Consensus, LlmReview, MarketContext


class FakeClient:
    """Duck-typed MT5Client with broker-like XAUUSD specs (1 lot = 100 oz)."""

    def prepare(self, symbol):
        return self.symbol_info(symbol)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            volume_min=0.01, volume_max=50.0, volume_step=0.01,
            trade_contract_size=100.0, trade_tick_value=1.0,
            trade_tick_size=0.01, point=0.01, digits=2,
            trade_stops_level=0, trade_freeze_level=0, trade_mode=4,
        )

    def tick(self, symbol):
        return SimpleNamespace(bid=2000.0, ask=2000.2)


def _cfg(require: bool) -> dict:
    return {
        "trading": {"require_llm_approval": require},
        "agents": {"atr_sl_mult": 1.5, "atr_tp_mult": 3.0},
        "risk": {
            "fixed_lot": 0.0,
            "risk_percent_per_trade": 0.5,
            "max_notional_leverage_per_position": 2.5,
        },
    }


def _plan(review: LlmReview, require: bool) -> object:
    agent = RiskAgent(_cfg(require), FakeClient())
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15", bid=2000.0,
                        ask=2000.2, point=0.01, digits=2, atr=5.0)
    consensus = Consensus(direction=1, strength=0.5, confidence=0.7)
    account = AccountSnapshot(equity=100_000.0, balance=100_000.0)
    return agent.build_plan(ctx, None, _cfg(require)["agents"], consensus,
                            review, account, atr=5.0)


def test_advisory_veto_keeps_the_trade():
    plan = _plan(LlmReview(False, "veto", 0.0, "counter-evidence"), require=False)
    assert plan.volume > 0
    assert "advisory" in plan.reason


def test_required_veto_blocks_the_trade():
    plan = _plan(LlmReview(False, "veto", 0.0, "counter-evidence"), require=True)
    assert plan.volume == 0


def test_advisory_resize_halves_volume():
    plain = _plan(LlmReview(True, "approve", 1.0, ""), require=False)
    half = _plan(LlmReview(True, "resize", 0.5, "risky"), require=False)
    assert plain.volume > 0
    assert 0 < half.volume <= plain.volume * 0.51
    assert "resize" in half.reason


def test_advisory_zero_resize_never_zeroes_the_trade():
    plan = _plan(LlmReview(True, "resize", 0.0, "no size"), require=False)
    assert plan.volume > 0


def test_required_resize_applies():
    half = _plan(LlmReview(True, "resize", 0.5, "risky"), require=True)
    assert 0 < half.volume <= 0.34


def test_resize_cannot_increase_volume():
    plan = _plan(LlmReview(True, "resize", 1.5, "bigger"), require=True)
    plain = _plan(LlmReview(True, "approve", 1.0, ""), require=True)
    assert plan.volume <= plain.volume


def test_missing_llm_review_defaults_to_approval_when_advisory():
    # LlmReview() defaults are approved/approve; advisory mode must trade.
    plan = _plan(LlmReview(), require=False)
    assert plan.volume > 0
