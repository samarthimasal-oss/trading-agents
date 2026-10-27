"""Execution layer: places market orders with SL/TP, manages open positions
(breakeven + ATR trailing + partial profit at 1R + optional time stop),
closes on opposite signals, journals everything.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

from core.models import TradePlan
from core.mt5_client import MT5Client, mt5
from core.state_store import StateStore

log = logging.getLogger("executor")

_FILLED = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL,
           mt5.TRADE_RETCODE_PLACED)

_EXEC_STATE = os.path.join("state", "executor_state.json")


class Executor:
    def __init__(self, client: MT5Client, magic: int, risk_cfg: dict,
                 comment: str = "TradingAgents"):
        self.client = client
        self.magic = int(magic)
        self.risk_cfg = risk_cfg or {}
        self.comment = comment
        self.store: StateStore | None = None
        self._risk_state: dict[str, dict] = {}    # ticket -> initial risk / flags
        self._load_state()

    # ------------------------------------------------------------ order placement
    def place(self, plan: TradePlan) -> bool:
        if plan.volume <= 0:
            log.info("Skip %s (volume 0 after review/limits)", plan.symbol)
            return False
        info = self.client.prepare(plan.symbol)
        settings = MT5Client.symbol_settings(info)
        tick = self.client.tick(plan.symbol)
        # stale/crossed broker quote (e.g. ask=0 on a frozen feed): never send
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            detail = (f"refused {plan.action.upper()} {plan.volume} {plan.symbol}: "
                      f"invalid quote bid={tick.bid} ask={tick.ask}")
            log.warning(detail)
            if self.store:
                self.store.event(kind="ORDER_REJECTED", symbol=plan.symbol,
                                 detail=detail)
            return False
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
        # never close on a frozen/crossed feed — the request would be rejected
        # (or worse, priced at 0); retry on the next poll instead
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            detail = (f"refused close #{position.ticket} {position.symbol}: "
                      f"invalid quote bid={tick.bid} ask={tick.ask}")
            log.warning(detail)
            if self.store:
                self.store.event(kind="CLOSE_REJECTED", symbol=position.symbol,
                                 detail=detail)
            return False
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

    # ------------------------------------------------------ partial / time stop
    def _load_state(self) -> None:
        try:
            with open(_EXEC_STATE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._risk_state = {str(k): v for k, v in data.items()
                                    if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:                     # noqa: BLE001
            log.warning("executor state unreadable (ignored): %s", exc)

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(_EXEC_STATE) or ".", exist_ok=True)
            tmp = _EXEC_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._risk_state, fh)
            os.replace(tmp, _EXEC_STATE)
        except Exception:                            # noqa: BLE001
            log.warning("could not persist executor state", exc_info=True)

    def _track(self, pos) -> dict:
        """Initial risk distance per ticket (first time we see the position)."""
        key = str(pos.ticket)
        rec = self._risk_state.get(key)
        risk = abs(float(pos.price_open) - float(pos.sl)) if pos.sl else 0.0
        if not rec:
            partial = False
            try:
                if self.store:
                    partial = self.store.has_event("PARTIAL_CLOSE", f"#{pos.ticket}")
            except Exception:                        # noqa: BLE001
                partial = False
            rec = {"symbol": pos.symbol, "dir": 1 if pos.type == mt5.POSITION_TYPE_BUY else -1,
                   "risk": risk, "opened_at": float(pos.time or time.time()),
                   "partial": partial}
            self._risk_state[key] = rec
            if risk > 0:
                self._save_state()
        elif not rec.get("risk") and risk > 0:
            rec["risk"] = risk                     # SL was still unset on first sight
            self._save_state()
        return rec

    def _close_volume(self, position, volume: float, reason: str) -> bool:
        """Partial close: close ``volume`` lots, keep the position (and its SL/TP)."""
        info = self.client.prepare(position.symbol)
        tick = self.client.tick(position.symbol)
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            detail = (f"refused partial #{position.ticket} {position.symbol}: "
                      f"invalid quote bid={tick.bid} ask={tick.ask}")
            log.warning(detail)
            if self.store:
                self.store.event(kind="PARTIAL_REJECTED", symbol=position.symbol,
                                 detail=detail)
            return False
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": round(float(volume), 8),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": 20,
            "magic": self.magic,
            "comment": (self.comment + "-partial")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        res = self.client.send_order(request)
        ret = int(res["retcode"])
        ok = ret in _FILLED
        detail = (f"#{position.ticket} {reason} {volume:g} lots @~"
                  f"{res.get('price') or request['price']}")
        if ok:
            log.info("PARTIAL CLOSE %s", detail)
        else:
            log.warning("partial close rejected retcode=%s: %s", ret, detail)
        if self.store:
            self.store.event(kind="PARTIAL_CLOSE" if ok else "PARTIAL_REJECTED",
                             symbol=position.symbol, detail=detail, retcode=ret,
                             price=res.get("price") or request["price"], volume=volume)
        return ok

    def _partial_volume(self, pos, settings: dict, pct: float) -> float:
        step = float(settings["volume_step"]) or 0.01
        vmin = float(settings["volume_min"])
        raw = float(pos.volume) * pct / 100.0
        vol = math.floor(raw / step + 1e-9) * step
        vol = round(vol, 6)
        if vol < vmin - 1e-9:
            return 0.0
        if float(pos.volume) - vol < vmin - 1e-9:  # keep the position open
            return 0.0
        return vol

    # ------------------------------------------ position maintenance (per poll)
    def manage(self, ctx) -> int:
        """Partial profit, time stop, breakeven lock and ATR trailing.

        Returns the number of SL changes. Partial closes / time stops are
        journaled as events (``PARTIAL_CLOSE`` / ``TIME_STOP``).
        """
        positions = self.bot_positions(ctx.symbol)
        if not positions or not ctx.atr or ctx.atr <= 0:
            return 0
        # a frozen/crossed feed makes every distance (trailing, 1R, buffer)
        # meaningless — leave the position untouched until quotes recover
        if ctx.bid <= 0 or ctx.ask <= 0 or ctx.ask < ctx.bid:
            return 0
        info = self.client.prepare(ctx.symbol)
        s = MT5Client.symbol_settings(info)
        digits = s["digits"]
        point = s["point"]
        min_dist = max(s["stops_level"], s["freezing_level"]) * point or 2 * point
        r = self.risk_cfg
        trail = float(r.get("trailing_atr", 1.0)) * ctx.atr
        be_trigger = float(r.get("breakeven_atr", 1.0)) * ctx.atr
        partial_on = bool(r.get("partial_close_enabled", True))
        partial_r = float(r.get("partial_close_at_r", 1.0) or 1.0)
        partial_pct = float(r.get("partial_close_pct", 50.0) or 50.0)
        time_stop_min = float(r.get("time_stop_minutes", 0.0) or 0.0)

        changes = 0
        active_tickets = set()
        for pos in positions:
            long = pos.type == mt5.POSITION_TYPE_BUY
            cur = ctx.bid if long else ctx.ask
            profit_dist = (cur - pos.price_open) if long else (pos.price_open - cur)
            candidates = []
            ticket = str(pos.ticket)
            active_tickets.add(ticket)
            tracked = self._track(pos)

            # ---- optional time stop: close positions that never worked out
            if time_stop_min > 0 and tracked.get("opened_at"):
                age_min = (time.time() - float(tracked["opened_at"])) / 60.0
                if age_min >= time_stop_min:
                    if self.close_position(pos):
                        if self.store:
                            self.store.event(
                                kind="TIME_STOP", symbol=pos.symbol,
                                detail=f"#{pos.ticket} closed after {age_min:.0f} min")
                        log.info("TIME STOP #%s %s after %.0f min", pos.ticket,
                                 pos.symbol, age_min)
                    self._risk_state.pop(ticket, None)
                    self._save_state()
                    continue

            # ---- partial profit at 1R: book half, then protect the rest
            forced_be = False
            risk_dist = float(tracked.get("risk") or 0.0)
            partial_done = bool(tracked.get("partial"))
            if (partial_on and not partial_done and risk_dist > 0
                    and profit_dist >= partial_r * risk_dist):
                vol = self._partial_volume(pos, s, partial_pct)
                if vol > 0 and self._close_volume(
                        pos, vol, f"{partial_pct:g}% at {partial_r:g}R"):
                    tracked["partial"] = True
                    self._save_state()
                    forced_be = True

            # breakeven lock: entry +/- small buffer once P/L clears be_trigger
            if profit_dist >= be_trigger or forced_be:
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

        stale = [k for k, v in self._risk_state.items()
                 if v.get("symbol") == ctx.symbol and k not in active_tickets]
        for k in stale:
            self._risk_state.pop(k, None)
        if stale:
            self._save_state()
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
