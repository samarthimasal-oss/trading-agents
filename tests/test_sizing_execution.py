"""Sizing / execution / guardian regressions.

These tests use duck-typed broker clients: no terminal, no network.  They pin
the properties that matter for account safety:

* volume is derived from the *same* stop the executor places, using the
  broker's own profit calculator, and is rounded DOWN
* a minimum lot that would exceed the risk budget is rejected, not clamped up
* margin is checked before sending and the trade is reduced or rejected
* a ``PLACED`` pending order is never reported as a fill
* duplicate entries on a symbol are refused by the executor
* a short position with no SL gets a stop (``min([0, ...])`` regression)
* failed SL modifications are not counted as changes
* the Guardian fails closed on account errors and reloads external resets
* daily bot P/L ignores other magics and account-wide floating profit
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agents.risk_agent import RiskAgent
from core.models import (AccountSnapshot, Consensus, LlmReview, MarketContext,
                         TradePlan)
from core.mt5_client import MT5Client, MT5Error, mt5
from execution.executor import Executor
from execution.guardian import Guardian


# --------------------------------------------------------------------- helpers
def _info(**over):
    base = dict(volume_min=0.01, volume_max=50.0, volume_step=0.01,
                trade_contract_size=100.0, trade_tick_value=1.0,
                trade_tick_size=0.01, point=0.01, digits=2,
                trade_stops_level=0, trade_freeze_level=0, trade_mode=4,
                filling_mode=1)
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(require: bool = False, risk: dict | None = None,
         agents: dict | None = None) -> dict:
    cfg = {
        "trading": {"require_llm_approval": require},
        "agents": {"atr_sl_mult": 1.5, "atr_tp_mult": 3.0},
        "risk": {
            "fixed_lot": 0.0,
            "risk_percent_per_trade": 0.5,
            "max_notional_leverage_per_position": 0.0,
        },
    }
    cfg["risk"].update(risk or {})
    cfg["agents"].update(agents or {})
    return cfg


class RiskFakeClient:
    def __init__(self, info=None, bid=2000.0, ask=2000.2):
        self.info = info or _info()
        self.bid = bid
        self.ask = ask
        self.profit_calls: list[tuple] = []

    def prepare(self, symbol):
        return self.info

    def tick(self, symbol):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def profit_if_closed(self, symbol, side, volume, price_open, price_close):
        self.profit_calls.append((symbol, side, volume, price_open, price_close))
        sign = 1 if side == "buy" else -1
        return sign * (price_close - price_open) * 100.0 * volume

    def margin_required(self, symbol, side, volume, price):
        return volume * price * 100.0 / 100.0


def _plan(client=None, equity=10_000.0, free_margin=None, atr=2.0,
          require=False, risk=None) -> tuple[TradePlan, RiskFakeClient]:
    client = client or RiskFakeClient()
    cfg = _cfg(require=require, risk=risk)
    agent = RiskAgent(cfg, client)
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15", bid=client.bid,
                        ask=client.ask, point=0.01, digits=2, atr=atr)
    consensus = Consensus(direction=1, strength=0.6, confidence=0.7)
    account = AccountSnapshot(
        equity=equity, balance=equity,
        free_margin=equity if free_margin is None else free_margin)
    plan = agent.build_plan(ctx, None, cfg["agents"], consensus,
                            LlmReview(True, "approve", 1.0, "ok"),
                            account, atr=atr)
    return plan, client


# ------------------------------------------------------------------ risk sizing
def test_volume_matches_plan_stop_and_stays_inside_budget():
    plan, client = _plan()
    assert plan.volume == pytest.approx(0.16)          # 0.1666 -> floor, not 0.17
    assert client.profit_calls, "broker profit calculator was not used"
    _, _, _, _, sizing_sl = client.profit_calls[-1]
    assert sizing_sl == pytest.approx(plan.sl)         # same stop, not a second one
    loss = abs(client.profit_if_closed("XAUUSD", "buy", plan.volume,
                                       plan.entry_hint, plan.sl))
    assert loss <= 10_000.0 * 0.005 + 1e-6
    # one more step would exceed the $50 budget (0.17 * $300 = $51)
    assert abs(client.profit_if_closed("XAUUSD", "buy", plan.volume + 0.01,
                                       plan.entry_hint, plan.sl)) > 50.0


def test_minimum_lot_over_budget_is_rejected_not_clamped_up():
    plan, _ = _plan(equity=1_000.0, atr=10.0)          # 0.01 lot risks $15 > $5
    assert plan.volume == 0.0
    assert "min lot" in plan.reason
    assert "no tradeable size" in plan.reason


def test_margin_guard_rejects_when_minimum_lot_does_not_fit():
    plan, _ = _plan(free_margin=10.0)
    assert plan.volume == 0.0
    assert "free margin" in plan.reason


def test_fixed_lot_is_capped_by_the_risk_budget():
    plan, client = _plan(risk={"fixed_lot": 5.0})
    assert plan.volume > 0
    loss = abs(client.profit_if_closed("XAUUSD", "buy", plan.volume,
                                       plan.entry_hint, plan.sl))
    assert loss <= 50.0 + 1e-6
    assert "fixed lot" in plan.reason


# --------------------------------------------------------------------- executor
class ExecFakeClient:
    def __init__(self, open_positions=None, send_result=None, modify_result=None,
                 account=None):
        self.cfg = {"mt5": {"deviation_points": 30}}
        self.info = _info()
        self.open_positions = list(open_positions or [])
        self.send_result = send_result
        self.modify_result = modify_result or {"retcode": mt5.TRADE_RETCODE_DONE}
        self.account_snapshot = account or AccountSnapshot(
            equity=100_000.0, balance=100_000.0, free_margin=100_000.0,
            trade_allowed=True)
        self.sent: list[dict] = []
        self.modified: list[tuple] = []

    def prepare(self, symbol):
        return self.info

    def tick(self, symbol):
        return SimpleNamespace(bid=2000.0, ask=2000.2, time=int(time.time()))

    def positions(self, symbol=None, magic=None):
        rows = self.open_positions
        if symbol is not None:
            rows = [p for p in rows if p.symbol == symbol]
        if magic is not None:
            rows = [p for p in rows if getattr(p, "magic", 0) == magic]
        return list(rows)

    def account(self):
        return self.account_snapshot

    def send_order(self, request):
        self.sent.append(dict(request))
        if self.send_result is not None:
            result = dict(self.send_result)
            result.setdefault("volume", request["volume"])
            result.setdefault("price", request["price"])
            return result
        return {"retcode": mt5.TRADE_RETCODE_DONE, "deal": 1,
                "volume": request["volume"], "price": request["price"]}

    def margin_required(self, symbol, side, volume, price):
        return volume * price * 100.0 / 100.0

    def modify_position(self, ticket, symbol, sl, tp, magic=None):
        self.modified.append((ticket, sl, tp))
        return dict(self.modify_result)


def _pos(ticket=1, side="buy", sl=1995.0, tp=2010.0, price_open=2000.0,
         volume=0.1, opened=None):
    return SimpleNamespace(
        ticket=ticket, symbol="XAUUSD",
        type=mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL,
        sl=sl, tp=tp, price_open=price_open, volume=volume,
        time=opened if opened is not None else time.time(), magic=260101)


def _trade_plan(volume=0.1, sl=1995.0, tp=2010.0, action="buy") -> TradePlan:
    return TradePlan(symbol="XAUUSD", action=action,
                     direction=1 if action == "buy" else -1,
                     volume=volume, sl=sl, tp=tp, atr=2.0, entry_hint=2000.2)


def _executor(monkeypatch, tmp_path, client) -> Executor:
    monkeypatch.setattr("execution.executor._EXEC_STATE", str(tmp_path / "exec.json"))
    ex = Executor(client, 260101, {"max_open_same_symbol": 1})
    ex._risk_state = {}
    return ex


def test_place_rounds_down_to_broker_step(monkeypatch, tmp_path):
    client = ExecFakeClient()
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan(volume=0.017)) is True
    assert client.sent[0]["volume"] == pytest.approx(0.01)
    assert client.sent[0]["deviation"] == 30       # from mt5.deviation_points


def test_place_rejects_volume_below_minimum(monkeypatch, tmp_path):
    client = ExecFakeClient()
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan(volume=0.009)) is False
    assert client.sent == []


def test_place_blocks_duplicate_symbol_position(monkeypatch, tmp_path):
    client = ExecFakeClient(open_positions=[_pos()])
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan()) is False
    assert client.sent == []
    assert "duplicate" in ex.last_order["detail"]


def test_placed_pending_order_is_not_a_fill(monkeypatch, tmp_path):
    client = ExecFakeClient(send_result={"retcode": mt5.TRADE_RETCODE_PLACED})
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan()) is False
    assert ex.last_order["kind"] == "ORDER_PLACED"
    assert ex.last_order["filled"] is False


def test_done_without_volume_or_visible_position_is_not_a_fill(monkeypatch, tmp_path):
    client = ExecFakeClient(send_result={"retcode": mt5.TRADE_RETCODE_DONE,
                                         "volume": 0.0})
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan()) is False
    assert ex.last_order["kind"] == "ORDER_REJECTED"


def test_place_rejects_stop_on_wrong_side(monkeypatch, tmp_path):
    client = ExecFakeClient()
    ex = _executor(monkeypatch, tmp_path, client)
    assert ex.place(_trade_plan(sl=2001.0)) is False   # buy SL above price
    assert client.sent == []


def test_manage_short_without_sl_gets_a_stop(monkeypatch, tmp_path):
    pos = _pos(side="sell", sl=0.0, tp=1990.0, price_open=2000.0)
    client = ExecFakeClient(open_positions=[pos])
    ex = _executor(monkeypatch, tmp_path, client)
    ex.risk_cfg.update({"trailing_atr": 1.0, "breakeven_atr": 1.0,
                        "trailing_step_atr": 0.25,
                        "partial_close_enabled": False})
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15", bid=1990.0,
                        ask=1990.2, point=0.01, digits=2, atr=2.0,
                        spread_points=20.0)
    changes = ex.manage(ctx)
    assert changes == 1
    assert client.modified, "no SL modification was sent"
    _, new_sl, _ = client.modified[0]
    assert 1990.02 < new_sl < 2000.0


def test_failed_sl_modification_is_not_counted(monkeypatch, tmp_path):
    pos = _pos(sl=1995.0)
    client = ExecFakeClient(open_positions=[pos],
                            modify_result={"retcode": 10013,
                                           "comment": "invalid request"})
    ex = _executor(monkeypatch, tmp_path, client)
    ex.risk_cfg.update({"trailing_atr": 1.0, "breakeven_atr": 1.0,
                        "trailing_step_atr": 0.25,
                        "partial_close_enabled": False})
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15", bid=2010.0,
                        ask=2010.2, point=0.01, digits=2, atr=2.0,
                        spread_points=20.0)
    assert ex.manage(ctx) == 0
    assert client.modified, "the modification should still have been attempted"


def test_time_stop_keeps_risk_record_when_close_fails(monkeypatch, tmp_path):
    pos = _pos(opened=time.time() - 3600)
    client = ExecFakeClient(
        open_positions=[pos],
        send_result={"retcode": mt5.TRADE_RETCODE_REQUOTE, "volume": 0.0})
    ex = _executor(monkeypatch, tmp_path, client)
    ex.risk_cfg.update({"time_stop_minutes": 1.0,
                        "partial_close_enabled": False})
    ctx = MarketContext(symbol="XAUUSD", timeframe="M15", bid=2000.0,
                        ask=2000.2, point=0.01, digits=2, atr=2.0,
                        spread_points=20.0)
    ex.manage(ctx)
    assert str(pos.ticket) in ex._risk_state


# --------------------------------------------------------------------- guardian
class BrokenAccountClient:
    def account(self):
        raise MT5Error("terminal offline")

    def daily_bot_profit(self, magic):
        return 0.0


def test_guardian_fails_closed_when_account_is_unavailable():
    g = Guardian({"risk": {"max_daily_loss_percent": 3.0,
                           "max_equity_drawdown_percent": 15.0}},
                 BrokenAccountClient(), None)
    g.magic = 1
    allowed, reason, flatten = g.check()
    assert allowed is False
    assert "paused" in reason
    assert flatten is False


def test_guardian_reloads_external_reset(tmp_path):
    from core.state_store import StateStore

    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        g = Guardian({"risk": {"max_daily_loss_percent": 3.0,
                               "max_equity_drawdown_percent": 15.0}},
                     type("C", (), {
                         "account": lambda self: AccountSnapshot(
                             equity=10_000.0, balance=10_000.0),
                         "daily_bot_profit": lambda self, magic: 0.0,
                     })(), store)
        g.magic = 1
        assert g.check()[0] is True
        g._halted_day = g.utc_day()
        g._halt_reason = "limit"
        g._persist()
        assert g.check()[0] is False
        # the API reset writes the DB directly; the running instance must see it
        store.save_guardian_state(day=g.utc_day(), day_start_equity=10_000.0,
                                  halted_day=None, halt_reason="", halted_at=None)
        assert g.check()[0] is True
    finally:
        store.close()


# ------------------------------------------------------------------ daily P/L
def test_daily_bot_profit_is_bot_only_and_includes_entry_commission(monkeypatch):
    import core.mt5_client as mc

    class FakeMT5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3

        @staticmethod
        def history_deals_get(start, end):
            return [
                SimpleNamespace(magic=1, entry=0, profit=0.0, swap=0.0,
                                commission=-2.0, fee=0.0),
                SimpleNamespace(magic=1, entry=1, profit=10.0, swap=1.0,
                                commission=-1.0, fee=0.0),
                SimpleNamespace(magic=2, entry=1, profit=100.0, swap=0.0,
                                commission=0.0, fee=0.0),
            ]

        @staticmethod
        def positions_get():
            return [SimpleNamespace(magic=1, profit=5.0, swap=0.0),
                    SimpleNamespace(magic=2, profit=100.0, swap=0.0)]

    client = MT5Client({})
    client.connected = True
    monkeypatch.setattr(mc, "mt5", FakeMT5)
    # -2 entry commission + 10 profit +1 swap -1 exit commission +5 floating
    assert client.daily_bot_profit(1) == pytest.approx(13.0)
