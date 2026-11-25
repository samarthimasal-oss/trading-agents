"""Execution layer: places market orders with SL/TP, manages open positions
(breakeven + ATR trailing + partial profit at 1R + optional time stop),
closes on opposite signals, journals everything.

Safety rules enforced here (broker responses are the source of truth):
  * only ``DONE`` / ``DONE_PARTIAL`` count as a filled market deal;
    ``PLACED`` is a pending order and is never reported as a fill
  * volume is re-validated against the live symbol spec and rounded DOWN
  * a symbol already at its configured position limit is rejected (duplicate
    entries after a restart or a race cannot stack risk)
  * free margin is checked before sending, then reduced to the broker minimum
  * SL/TP must be on the correct side of the market
  * every retcode, including failures, is journaled
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

from core.models import TradePlan
from core.mt5_client import MT5Client, MT5Error, mt5
from core.state_store import StateStore

log = logging.getLogger("executor")

# A market deal is filled only by these retcodes.  PLACED means the broker
# accepted a *pending* order: no position exists yet, so it is not success.
_DEAL_FILLED = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
_PLACED = mt5.TRADE_RETCODE_PLACED
_NO_CHANGE = getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025)

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
        self.last_order: dict = {}
        self._load_state()

    # --------------------------------------------------------------- utilities
    def _deviation(self) -> int:
        cfg = getattr(self.client, "cfg", {}) or {}
        try:
            return max(1, int((cfg.get("mt5") or {}).get("deviation_points") or 20))
        except (TypeError, ValueError):
            return 20

    @staticmethod
    def _normalize_volume(volume: float, settings: dict) -> float:
        """Floor ``volume`` to the symbol step; 0 when below the minimum."""
        step = float(settings.get("volume_step") or 0.01)
        vmin = float(settings.get("volume_min") or step)
        vmax = float(settings.get("volume_max") or volume)
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(volume) or volume < vmin - 1e-9:
            return 0.0
        vol = math.floor(volume / step + 1e-9) * step
        vol = min(vol, vmax)
        if vol < vmin - 1e-9:
            return 0.0
        return round(vol, 8)

    def _reject(self, kind: str, symbol: str, detail: str,
                retcode: int = 0, price: float = 0.0,
                volume: float = 0.0) -> bool:
        log.warning("%s: %s", kind, detail)
        if self.store:
            self.store.event(kind=kind, symbol=symbol, detail=detail,
                             retcode=retcode, price=price, volume=volume)
        self.last_order = {"symbol": symbol, "filled": False,
                           "kind": kind, "detail": detail, "retcode": retcode}
        return False

    def _find_new_position(self, symbol: str, before: set):
        try:
            current = self.bot_positions(symbol)
        except Exception:                            # noqa: BLE001
            return None
        for pos in current:
            if int(getattr(pos, "ticket", 0)) not in before:
                return pos
        return None

    # ------------------------------------------------------------ order placement
    def place(self, plan: TradePlan) -> bool:
        self.last_order = {"symbol": plan.symbol, "action": plan.action,
                           "requested_volume": plan.volume, "filled": False}
        if plan.volume <= 0:
            log.info("Skip %s (volume 0 after review/limits)", plan.symbol)
            return False
        if plan.action not in ("buy", "sell"):
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"invalid action {plan.action!r}")
        try:
            info = self.client.prepare(plan.symbol)
            tick = self.client.tick(plan.symbol)
        except MT5Error as exc:
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"broker data unavailable: {exc}")
        settings = MT5Client.symbol_settings(info)
        # stale/crossed broker quote (e.g. ask=0 on a frozen feed): never send
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            return self._reject(
                "ORDER_REJECTED", plan.symbol,
                f"invalid quote bid={tick.bid} ask={tick.ask}")

        side = plan.action
        price = tick.ask if side == "buy" else tick.bid

        # hard safety: a market entry must carry a protective stop on the
        # correct side; a malformed plan is rejected before it reaches MT5
        if plan.sl <= 0:
            return self._reject("ORDER_REJECTED", plan.symbol,
                                "missing stop-loss on market entry")
        if side == "buy" and plan.sl >= price:
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"buy SL {plan.sl} not below entry {price}")
        if side == "sell" and plan.sl <= price:
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"sell SL {plan.sl} not above entry {price}")

        mode = int(settings.get("trade_mode", 4) or 0)
        if mode == 0 or mode == 3:       # disabled / close-only
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"symbol trading not available (mode={mode})")
        if (side == "buy" and mode == 2) or (side == "sell" and mode == 1):
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"symbol mode {mode} does not allow {side}")

        volume = self._normalize_volume(plan.volume, settings)
        if volume <= 0:
            return self._reject(
                "ORDER_REJECTED", plan.symbol,
                f"volume {plan.volume:g} below broker minimum "
                f"{settings['volume_min']:g} (step {settings['volume_step']:g})")

        # duplicate-entry guard: the risk gate is not enough after a restart,
        # a manual entry or a race between polls
        existing = self.bot_positions(plan.symbol)
        max_same = int(self.risk_cfg.get("max_open_same_symbol", 1) or 1)
        if len(existing) >= max_same:
            return self._reject(
                "ORDER_REJECTED", plan.symbol,
                f"duplicate entry blocked: {len(existing)} position(s) already "
                f"open (limit {max_same})")

        # account permission + margin (final guard before the broker)
        try:
            account = self.client.account()
        except MT5Error as exc:
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"account snapshot unavailable: {exc}")
        if not bool(getattr(account, "trade_allowed", True)):
            return self._reject("ORDER_REJECTED", plan.symbol,
                                "account trading is disabled")
        free = float(getattr(account, "free_margin", 0.0) or 0.0)
        if free <= 0:
            free = float(getattr(account, "equity", 0.0) or 0.0)
        margin_fn = getattr(self.client, "margin_required", None)
        if callable(margin_fn) and free > 0:
            step = float(settings["volume_step"] or 0.01)
            budget = free * 0.95
            capped = False
            while volume > 0:
                try:
                    margin = margin_fn(plan.symbol, side, volume, price)
                except Exception:                    # noqa: BLE001
                    margin = None
                if margin is None or margin <= budget:
                    break
                capped = True
                volume = self._normalize_volume(volume - step, settings)
            if volume <= 0:
                return self._reject(
                    "ORDER_REJECTED", plan.symbol,
                    f"insufficient free margin {free:.2f} even for "
                    f"{settings['volume_min']:g} lots")
            if capped:
                log.info("%s volume reduced to %.3f by the margin guard",
                         plan.symbol, volume)

        # respect broker minimum stop distance (never widen risk blindly:
        # the risk agent already sized against a stop at least this wide)
        min_dist = max(settings["stops_level"], settings["freezing_level"]) * settings["point"]
        if side == "buy":
            sl = min(plan.sl, price - min_dist) if min_dist else plan.sl
            tp = max(plan.tp, price + min_dist) if min_dist else plan.tp
        else:
            sl = max(plan.sl, price + min_dist) if min_dist else plan.sl
            tp = min(plan.tp, price - min_dist) if min_dist else plan.tp
        sl = round(sl, settings["digits"])
        tp = round(tp, settings["digits"])
        if (side == "buy" and (sl >= price or tp <= price)) or \
                (side == "sell" and (sl <= price or tp >= price)):
            return self._reject("ORDER_REJECTED", plan.symbol,
                                f"invalid SL/TP after broker distance: "
                                f"sl={sl} tp={tp} price={price}")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": plan.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self._deviation(),
            "magic": self.magic,
            "comment": self.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        before = {int(getattr(p, "ticket", 0)) for p in existing}
        detail = (f"{side.upper()} {volume:g} {plan.symbol} @ ~{price:.{info.digits}f} "
                  f"sl={sl} tp={tp}")
        try:
            res = self.client.send_order(request)
        except MT5Error as exc:
            # the response was lost: check whether the broker actually opened
            # the position before reporting failure (avoids duplicate retries)
            pos = self._find_new_position(plan.symbol, before)
            if pos is not None:
                self.store and self.store.event(
                    kind="ORDER_FILLED", symbol=plan.symbol,
                    detail=f"{detail} (confirmed by position after: {exc})",
                    price=float(getattr(pos, "price_open", price) or price),
                    volume=float(getattr(pos, "volume", volume) or volume))
                self.last_order = {"symbol": plan.symbol, "filled": True,
                                   "volume": float(getattr(pos, "volume", volume)),
                                   "price": float(getattr(pos, "price_open", price) or price),
                                   "detail": detail}
                return True
            return self._reject("ORDER_REJECTED", plan.symbol, f"send failed: {exc}")

        ret = int(res.get("retcode", -1))
        if ret == _PLACED:
            return self._reject(
                "ORDER_PLACED", plan.symbol,
                f"{detail} (pending order accepted, not filled)", retcode=ret,
                price=float(res.get("price") or price), volume=volume)
        filled = ret in _DEAL_FILLED
        actual_volume = float(res.get("volume") or 0.0)
        fill_price = float(res.get("price") or price)
        if filled and actual_volume <= 0:
            # DONE with no volume: only trust a visible position
            pos = self._find_new_position(plan.symbol, before)
            if pos is None:
                filled = False
                detail += " (DONE without deal volume or visible position)"
            else:
                actual_volume = float(getattr(pos, "volume", volume) or volume)
                fill_price = float(getattr(pos, "price_open", fill_price) or fill_price)
        if filled:
            log.info("FILLED %s (deal=%s actual=%s)", detail, res.get("deal"),
                     actual_volume or volume)
            kind = "ORDER_FILLED"
        else:
            log.warning("order rejected retcode=%s: %s", ret, detail)
            kind = "ORDER_REJECTED"
        if self.store:
            self.store.event(kind=kind, symbol=plan.symbol, detail=detail,
                             retcode=ret, price=fill_price,
                             volume=actual_volume or volume)
        self.last_order = {"symbol": plan.symbol, "filled": filled,
                           "kind": kind, "volume": actual_volume or volume,
                           "price": fill_price, "retcode": ret,
                           "detail": detail}
        return filled

    # ------------------------------------------------------------ position tools
    def bot_positions(self, symbol: str | None = None) -> list:
        return self.client.positions(symbol=symbol, magic=self.magic)

    def close_position(self, position) -> bool:
        info = self.client.prepare(position.symbol)
        tick = self.client.tick(position.symbol)
        # never close on a frozen/crossed feed — the request would be rejected
        # (or worse, priced at 0); retry on the next poll instead
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            return self._reject(
                "CLOSE_REJECTED", position.symbol,
                f"refused close #{position.ticket}: invalid quote "
                f"bid={tick.bid} ask={tick.ask}")
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self._deviation(),
            "magic": self.magic,
            "comment": (self.comment + "-close")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        res = self.client.send_order(request)
        ret = int(res.get("retcode", -1))
        ok = ret in _DEAL_FILLED
        detail = f"close #{position.ticket} @~{res.get('price') or request['price']}"
        if ok:
            log.info("CLOSED #%s %s", position.ticket, position.symbol)
            if self.store:
                self.store.event(kind="ORDER_CLOSED", symbol=position.symbol,
                                 detail=detail, retcode=ret,
                                 price=float(res.get("price") or request["price"]),
                                 volume=float(res.get("volume") or position.volume))
        else:
            self._reject("CLOSE_REJECTED", position.symbol,
                         f"{detail} retcode={ret}", retcode=ret,
                         price=float(res.get("price") or request["price"]),
                         volume=float(position.volume))
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
            rec = {"symbol": pos.symbol,
                   "dir": 1 if pos.type == mt5.POSITION_TYPE_BUY else -1,
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
            return self._reject(
                "PARTIAL_REJECTED", position.symbol,
                f"refused partial #{position.ticket}: invalid quote "
                f"bid={tick.bid} ask={tick.ask}")
        if volume <= 0 or float(volume) >= float(position.volume) - 1e-9:
            return self._reject(
                "PARTIAL_REJECTED", position.symbol,
                f"refused partial #{position.ticket}: volume {volume:g} "
                f"not smaller than position {position.volume:g}")
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": round(float(volume), 8),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self._deviation(),
            "magic": self.magic,
            "comment": (self.comment + "-partial")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": MT5Client._pick_filling(info),
        }
        res = self.client.send_order(request)
        ret = int(res.get("retcode", -1))
        ok = ret in _DEAL_FILLED
        detail = (f"#{position.ticket} {reason} {volume:g} lots @~"
                  f"{res.get('price') or request['price']}")
        if ok:
            log.info("PARTIAL CLOSE %s", detail)
        else:
            log.warning("partial close rejected retcode=%s: %s", ret, detail)
        if self.store:
            self.store.event(kind="PARTIAL_CLOSE" if ok else "PARTIAL_REJECTED",
                             symbol=position.symbol, detail=detail, retcode=ret,
                             price=float(res.get("price") or request["price"]),
                             volume=float(volume))
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

        Returns the number of *successful* SL changes.  Partial closes / time
        stops are journaled as events (``PARTIAL_CLOSE`` / ``TIME_STOP``).
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
        trail_step = max(float(r.get("trailing_step_atr", 0.0) or 0.0) * ctx.atr,
                         min_dist)
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
                        # only forget the risk record when the broker confirmed
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
            else:
                if pos.sl == 0 or trailing < pos.sl:
                    candidates.append(trailing)

            # never let a zero SL win the min()/max() comparison (shorts used
            # to keep sl=0 forever because min([0, ...]) == 0)
            levels = [float(pos.sl)] if float(pos.sl or 0.0) > 0 else []
            levels.extend(candidates)
            if not levels:
                continue
            if long:
                new_sl = max(levels)
                if pos.sl and new_sl - pos.sl < trail_step - 1e-9:
                    continue
                valid = new_sl < cur - min_dist
            else:
                new_sl = min(levels)
                if pos.sl and pos.sl - new_sl < trail_step - 1e-9:
                    continue
                valid = new_sl > cur + min_dist
            if not (valid and new_sl > 0):
                continue
            if self._modify_sl(pos, round(new_sl, digits)):
                changes += 1

        stale = [k for k, v in self._risk_state.items()
                 if v.get("symbol") == ctx.symbol and k not in active_tickets]
        for k in stale:
            self._risk_state.pop(k, None)
        if stale:
            self._save_state()
        return changes

    # ------------------------------------------------------------ SL modification
    def _modify_sl(self, position, new_sl: float) -> bool:
        try:
            result = self.client.modify_position(
                position.ticket, position.symbol, new_sl,
                float(position.tp or 0.0), self.magic)
        except MT5Error as exc:
            log.warning("SL modify failed #%s -> %s: %s",
                        position.ticket, new_sl, exc)
            if self.store:
                self.store.event(kind="SL_REJECTED", symbol=position.symbol,
                                 detail=f"#{position.ticket} -> {new_sl}: {exc}")
            return False
        ret = int(result.get("retcode", -1))
        if ret in _DEAL_FILLED:
            log.info("SL -> %.5f on #%s %s", new_sl, position.ticket, position.symbol)
            if self.store:
                self.store.event(kind="SL_MODIFIED", symbol=position.symbol,
                                 detail=f"#{position.ticket} sl->{new_sl}",
                                 retcode=ret)
            return True
        if ret == _NO_CHANGE:
            log.debug("SL already at %.5f on #%s", new_sl, position.ticket)
            return False
        log.warning("SL modify rejected #%s -> %s (retcode=%s %s)",
                    position.ticket, new_sl, ret, result.get("comment", ""))
        if self.store:
            self.store.event(kind="SL_REJECTED", symbol=position.symbol,
                             detail=f"#{position.ticket} -> {new_sl} "
                                    f"retcode={ret}", retcode=ret)
        return False
