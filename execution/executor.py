"""Execution layer: places market orders with SL/TP, manages open positions
(breakeven + ATR trailing), closes on opposite signals, journals everything.
"""
from __future__ import annotations

import logging

from core.models import TradePlan
from core.mt5_client import MT5Client, mt5
from core.state_store import StateStore

log = logging.getLogger("executor")

_FILLED = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL,
           mt5.TRADE_RETCODE_PLACED)


class Executor:
    def __init__(self, client: MT5Client, magic: int, risk_cfg: dict,
                 comment: str = "TradingAgents"):
        self.client = client
        self.magic = int(magic)
        self.risk_cfg = risk_cfg or {}
        self.comment = comment
        self.store: StateStore | None = None

    # ------------------------------------------------------------ order placement
    def place(self, plan: TradePlan) -> bool:
        if plan.volume <= 0:
            log.info("Skip %s (volume 0 after review/limits)", plan.symbol)
            return False
        info = self.client.prepare(plan.symbol)
        settings = MT5Client.symbol_settings(info)
        tick = self.client.tick(plan.symbol)
        side = plan.action
        price = tick.ask if side == "buy" else tick.bid

        # respect broker minimum stop distance
        min_dist = max(settings["stops_level"], settings["freezing_level"]) * settings["point"]
        if side == "buy":
            sl = min(plan.sl, price - min_dist) if min_dist else plan.sl
            tp = max(plan.tp, price + min_dist) if min_dist else plan.tp
        else:
            sl = max(plan.sl, price + min_dist) if min_dist else plan.sl
            tp = min(plan.tp, price - min_dist) if min_dist else plan.tp

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": plan.symbol,
            "volume": plan.volume,
            "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(sl, settings["digits"]),
            "tp": round(tp, settings["digits"]),
            "deviation": 20,
            "magic": self.magic,
            "comment": self.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        res = self.client.send_order(request)
        ret = int(res["retcode"])
        ok = ret in _FILLED
        detail = (f"{side.upper()} {plan.volume} {plan.symbol} @ ~{price:.{info.digits}f} "
                  f"sl={request['sl']} tp={request['tp']}")
        if ok:
            log.info("FILLED %s (deal=%s)", detail, res.get("deal"))
        else:
            log.warning("order rejected retcode=%s: %s", ret, detail)
        if self.store:
            self.store.event(kind="ORDER_FILLED" if ok else "ORDER_REJECTED",
                             symbol=plan.symbol, detail=detail, retcode=ret,
                             price=res.get("price") or price, volume=plan.volume)
        return ok

    # ------------------------------------------------------------ position tools
    def bot_positions(self, symbol: str | None = None) -> list:
        return self.client.positions(symbol=symbol, magic=self.magic)

    def close_position(self, position) -> bool:
        info = self.client.prepare(position.symbol)
        tick = self.client.tick(position.symbol)
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": 20,
            "magic": self.magic,
            "comment": (self.comment + "-close")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        res = self.client.send_order(request)
        ret = int(res["retcode"])
        ok = ret in _FILLED
        if ok and self.store:
            self.store.event(kind="ORDER_CLOSED", symbol=position.symbol,
                             detail=f"close #{position.ticket} @~{res.get('price')}",
                             retcode=ret, volume=position.volume)
        if ok:
            log.info("CLOSED #%s %s", position.ticket, position.symbol)
        return ok

    def close_all(self, symbol: str | None = None) -> int:
        closed = 0
        for pos in self.bot_positions(symbol):
            if self.close_position(pos):
                closed += 1
        return closed

    # ------------------------------------------ position maintenance (per poll)
    def manage(self, ctx) -> int:
        """Breakeven lock + ATR trailing stop. Returns number of SL changes."""
        positions = self.bot_positions(ctx.symbol)
        if not positions or not ctx.atr or ctx.atr <= 0:
            return 0
        info = self.client.prepare(ctx.symbol)
        s = MT5Client.symbol_settings(info)
        digits = s["digits"]
        point = s["point"]
        min_dist = max(s["stops_level"], s["freezing_level"]) * point or 2 * point
        r = self.risk_cfg
        trail = float(r.get("trailing_atr", 1.0)) * ctx.atr
        be_trigger = float(r.get("breakeven_atr", 1.0)) * ctx.atr

        changes = 0
        for pos in positions:
            long = pos.type == mt5.POSITION_TYPE_BUY
            cur = ctx.bid if long else ctx.ask
            profit_dist = (cur - pos.price_open) if long else (pos.price_open - cur)
            candidates = []

            # breakeven lock: entry +/- small buffer once P/L clears be_trigger
            if profit_dist >= be_trigger:
                buffer = min_dist + ctx.spread_points * point
                if long:
                    candidates.append(pos.price_open + buffer)
                else:
                    candidates.append(pos.price_open - buffer)

            # ATR trailing, only ever tightening
            trailing = (cur - trail) if long else (cur + trail)
            if long:
                if pos.sl == 0 or trailing > pos.sl:
                    candidates.append(trailing)
                new_sl = max([pos.sl] + candidates) if candidates else pos.sl
                tighter = new_sl > (pos.sl or 0) + 1e-9 or pos.sl == 0
                valid = new_sl < cur - min_dist  # SL below price
            else:
                if pos.sl == 0 or trailing < pos.sl:
                    candidates.append(trailing)
                new_sl = min([pos.sl] + candidates) if candidates else pos.sl
                tighter = (pos.sl == 0) or new_sl < pos.sl - 1e-9
                valid = new_sl > cur + min_dist  # SL above price

            if not (tighter and valid and new_sl > 0):
                continue
            self._modify_sl(pos, round(new_sl, digits))
            changes += 1
        return changes

    # ------------------------------------------------------------ SL modification
    _NO_CHANGE = 10025  # TRADE_RETCODE_NO_CHANGES: SL already at requested level

    def _modify_sl(self, position, new_sl: float) -> None:
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": new_sl,
            "tp": position.tp,
            "magic": self.magic,
        }
        result = mt5.order_send(request)
        ret = getattr(result, "retcode", None)
        if result is not None and (ret in _FILLED or ret == self._NO_CHANGE):
            log.debug("SL -> %.5f on #%s %s (retcode=%s)",
                      new_sl, position.ticket, position.symbol, ret)
            if ret in _FILLED:
                log.info("SL -> %.5f on #%s %s", new_sl, position.ticket, position.symbol)
                if self.store:
                    self.store.event(kind="SL_MODIFIED", symbol=position.symbol,
                                     detail=f"#{position.ticket} sl->{new_sl}")
        else:
            log.warning("SL modify failed #%s -> %s (retcode=%s)",
                        position.ticket, new_sl, ret)
