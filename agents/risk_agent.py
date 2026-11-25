"""Risk agent: turn a direction into a *sized, filtered, approved* trade plan.

Position sizing: risk a fixed % of equity against the ATR stop distance.
Filters: spread, position counts, trading hours, cooldown between trades.
The LLM review's size multiplier is applied here as well.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from core.models import MarketContext, AccountSnapshot, Consensus, LlmReview, TradePlan
from core.mt5_client import MT5Client
from core.quotes import spread_limit_for

log = logging.getLogger("risk")


@dataclass
class RiskGate:
    ok: bool
    reason: str = ""


class RiskAgent:
    def __init__(self, cfg: dict, client: MT5Client):
        self.cfg = cfg
        self.client = client
        self._last_entry_time: dict[str, float] = {}
        self._llm_last: dict[str, LlmReview] = {}
        self.last_sizing_note = ""

    def note_entry(self, symbol: str, when: float | None = None) -> None:
        """Record a successful entry so the per-symbol cooldown is enforced."""
        self._last_entry_time[symbol] = float(when if when is not None else time.time())

    # ------------------------------------------------------------------ sizing
    def _notional_usd_per_lot(self, symbol: str, info: dict, price: float) -> float:
        """Approximate USD value controlled by 1.0 lot — used by the leverage cap."""
        cs = float(info.get("trade_contract_size") or 0.0)
        if cs <= 0 or price <= 0:
            return 0.0
        sym = symbol.upper()
        quote = sym[-3:]
        if quote == "USD":          # EURUSD/XAUUSD: lots * contract * price
            return cs * price
        if sym[:3] == "USD":        # USDJPY: $100k per lot regardless of quote
            return cs
        return cs * price           # fallback: treat price as USD-denominated

    def size_position(self, ctx: MarketContext, info: dict, equity: float,
                      atr_val: float, *, sl_distance: float | None = None,
                      entry: float | None = None, side: int = 1,
                      free_margin: float | None = None) -> float:
        """Lots that keep the loss at the stop inside the risk budget.

        The stop distance is the *same* one the plan and executor use.  The
        money value of that distance comes from ``order_calc_profit`` when the
        terminal exposes it (contract size, tick value and currency conversion
        are broker facts, not guesses).  Volume is always rounded **down** to
        the broker step; when even the minimum lot would exceed the budget the
        trade is rejected instead of being clamped up into over-risk.
        """
        volume, note = self._size(ctx, info, equity, atr_val,
                                  sl_distance=sl_distance, entry=entry,
                                  side=side, free_margin=free_margin)
        self.last_sizing_note = note
        if note:
            log.info("%s sizing: %s", ctx.symbol, note)
        return volume

    def _size(self, ctx: MarketContext, info: dict, equity: float,
              atr_val: float, *, sl_distance: float | None, entry: float | None,
              side: int, free_margin: float | None) -> tuple[float, str]:
        risk_cfg = self.cfg["risk"]
        equity = max(0.0, float(equity or 0.0))
        risk_pct = float(risk_cfg.get("risk_percent_per_trade") or 0.0)
        risk_amount = equity * risk_pct / 100.0
        if risk_amount <= 0:
            return 0.0, "risk budget is zero"

        if sl_distance is None:
            sl_mult = float(self.cfg["agents"]["atr_sl_mult"])
            stop_dist = max(float(atr_val or 0.0) * sl_mult,
                            float(info["stops_level"]) * float(info["point"]) * 2.0)
        else:
            stop_dist = float(sl_distance)
        if stop_dist <= 0:
            return 0.0, "stop distance unavailable"

        if entry is None:
            entry = ctx.ask if side > 0 else ctx.bid
        entry = float(entry or 0.0)
        if entry <= 0:
            return 0.0, "entry price unavailable"

        sl_price = entry - stop_dist if side > 0 else entry + stop_dist
        loss_per_lot: float | None = None
        calc = getattr(self.client, "profit_if_closed", None)
        if callable(calc):
            try:
                value = calc(ctx.symbol, "buy" if side > 0 else "sell",
                             1.0, entry, sl_price)
            except Exception:                        # noqa: BLE001
                value = None
            if value is not None and math.isfinite(float(value)) and float(value) < 0:
                loss_per_lot = abs(float(value))
        if not loss_per_lot:
            tick_size = float(info.get("trade_tick_size") or info["point"] or 0.0)
            tick_value = float(info.get("trade_tick_value") or 0.0)
            if tick_size <= 0 or tick_value <= 0:
                return 0.0, "tick value unavailable"
            loss_per_lot = (stop_dist / tick_size) * tick_value
        commission = float(risk_cfg.get("commission_per_lot_round_turn") or 0.0)
        if commission > 0:
            loss_per_lot += commission
        if loss_per_lot <= 0:
            return 0.0, "loss per lot unavailable"

        max_by_risk = risk_amount / loss_per_lot
        note = ""
        fixed = float(risk_cfg.get("fixed_lot") or 0.0)
        if fixed > 0:
            if fixed > max_by_risk + 1e-9:
                note = (f"fixed lot {fixed:g} exceeds {risk_pct:g}% budget; "
                        f"capped at {max_by_risk:.3f}")
            raw = min(fixed, max_by_risk)
        else:
            raw = max_by_risk

        # notional leverage cap: never let one position control more than
        # max_notional_leverage_per_position x equity (tight ATR stops would
        # otherwise explode the volume — e.g. 13 lots on a 7-pip stop)
        lev_cap = float(risk_cfg.get("max_notional_leverage_per_position") or 0)
        if lev_cap > 0 and equity > 0:
            mid = 0.0
            if ctx.bid > 0 and ctx.ask > 0:
                mid = (ctx.bid + ctx.ask) / 2.0
            else:
                mid = ctx.bid or ctx.ask or 0.0
            notional_1lot = self._notional_usd_per_lot(ctx.symbol, info, mid)
            if notional_1lot > 0:
                lev_volume = equity * lev_cap / notional_1lot
                if lev_volume < raw:
                    note = (note + "; " if note else "") + f"leverage cap {lev_volume:.3f}"
                    raw = lev_volume

        volume = self._round_volume(raw, info)
        if volume <= 0:
            min_risk = float(info["volume_min"] or 0.0) * loss_per_lot
            return 0.0, (note + "; " if note else "") + (
                f"min lot {float(info['volume_min']):g} would risk "
                f"{min_risk:.2f} > budget {risk_amount:.2f}")

        # margin protection: shrink down to the broker minimum, then reject
        free = float(free_margin or 0.0)
        margin_fn = getattr(self.client, "margin_required", None)
        if callable(margin_fn) and free > 0:
            budget = free * 0.95
            step = float(info["volume_step"] or 0.01)
            capped = False
            while volume > 0:
                try:
                    margin = margin_fn(ctx.symbol, "buy" if side > 0 else "sell",
                                       volume, entry)
                except Exception:                    # noqa: BLE001
                    margin = None
                if margin is None or margin <= budget:
                    break
                capped = True
                volume = self._round_volume(volume - step, info)
            if volume <= 0:
                return 0.0, (note + "; " if note else "") + (
                    f"insufficient free margin ({free:.2f}) for the minimum lot")
            if capped:
                note += ("; " if note else "") + f"margin cap {volume:.3f}"
        return volume, note

    @staticmethod
    def _round_volume(volume: float, info: dict) -> float:
        """Round DOWN to the broker step; 0 means 'not tradeable'."""
        step = float(info["volume_step"] or 0.01)
        vmin = float(info["volume_min"] or step)
        vmax = float(info["volume_max"] or volume)
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

    # ------------------------------------------------------------------ gates
    def pre_trade_gate(self, ctx: MarketContext, account: AccountSnapshot,
                       open_positions: list) -> RiskGate:
        """Cheap filters that run even before consensus is computed."""
        r = self.cfg["risk"]
        # a stale/crossed broker quote (ask=0, bid=0) must never pass the gate:
        # the spread can even look negative and slip through a "> limit" check
        if ctx.bid <= 0 or ctx.ask <= 0 or ctx.spread_points < 0 or ctx.ask < ctx.bid:
            return RiskGate(False, f"invalid quote (bid {ctx.bid}, ask {ctx.ask})")
        spread_limit = spread_limit_for(ctx.symbol, r)
        if ctx.spread_points > spread_limit:
            return RiskGate(False, f"spread {ctx.spread_points:.0f} > {spread_limit:.0f} pts")

        symbol_pos = [p for p in open_positions if p.symbol == ctx.symbol]
        if len(symbol_pos) >= int(r["max_open_same_symbol"]):
            return RiskGate(False, f"already {len(symbol_pos)} position(s) on {ctx.symbol}")
        if len(open_positions) >= int(r["max_positions"]):
            return RiskGate(False, f"max_positions {r['max_positions']} reached")

        hours_lo, hours_hi = r.get("trading_hours_utc") or [0, 24]
        h = time.gmtime().tm_hour
        if not (hours_lo <= h < hours_hi):
            return RiskGate(False, f"outside trading hours UTC {hours_lo}-{hours_hi}")

        gap = float(r["min_gap_seconds_between_same_symbol_trades"] or 0)
        last = self._last_entry_time.get(ctx.symbol, 0.0)
        if gap and time.time() - last < gap:
            return RiskGate(False, f"cooldown {gap:.0f}s not elapsed")

        level = account.margin_level
        if level and level > 0 and level < 200:
            return RiskGate(False, f"margin level low ({level:.0f}%)")
        if account.equity <= 0:
            return RiskGate(False, "no equity")
        return RiskGate(True)

    # ------------------------------------------------------------- final shaping
    def build_plan(self, ctx: MarketContext, df, agent_cfg: dict,
                   consensus: Consensus, review: LlmReview,
                   account: AccountSnapshot, atr: float | None = None) -> TradePlan:
        """Compose the executable plan: entry, volume, SL, TP.

        ``atr`` should be the last **closed** bar's ATR.  When supplied it is
        authoritative; ``ctx.atr`` is only a fallback for callers that have
        already enforced closed-bar semantics.
        """
        symbol = ctx.symbol
        info = MT5Client.symbol_settings(self.client.prepare(symbol))
        atr_val = float(atr) if atr is not None else (ctx.atr or 0.0)
        if atr_val <= 0:
            return TradePlan(symbol, "none", reason="ATR unavailable")

        sl_mult = float(agent_cfg["atr_sl_mult"])
        tp_mult = float(agent_cfg["atr_tp_mult"])
        broker_min = info["stops_level"] * info["point"]
        stop_dist = max(atr_val * sl_mult, broker_min * 2.0)
        tp_dist = max(atr_val * tp_mult, broker_min * 1.5)
        tick = self.client.tick(symbol)
        side = 1 if consensus.direction > 0 else -1
        raw_entry = tick.ask if side > 0 else tick.bid
        if raw_entry <= 0:
            return TradePlan(symbol, "none", reason="broker quote unavailable")
        entry = round(raw_entry, info["digits"])
        if side > 0:
            sl, tp = entry - stop_dist, entry + tp_dist
        else:
            sl, tp = entry + stop_dist, entry - tp_dist
        sl, tp = round(sl, info["digits"]), round(tp, info["digits"])
        # size against the *actual* stop the executor will place (after
        # digit rounding), never a second independently computed distance
        volume = self.size_position(
            ctx, info, account.equity, atr_val,
            sl_distance=abs(entry - sl), entry=entry, side=side,
            free_margin=account.free_margin)
        sizing_note = self.last_sizing_note
        # Gemini is advisory unless approval is explicitly required.  An
        # advisory veto must not silently zero a valid trade; an advisory
        # resize may only reduce size (never below a quarter of the plan and
        # never to zero).  When approval IS required, a veto blocks the trade.
        required = bool(self.cfg.get("trading", {}).get("require_llm_approval", False))
        llm_note = ""
        if review.action == "resize" and review.approved:
            mult = min(max(float(review.size_multiplier), 0.0), 1.0)
            if not required:
                mult = max(mult, 0.25)
            resized = self._round_volume(volume * mult, info)
            if required:
                volume = resized
            elif resized > 0:
                volume = resized            # advisory resize may only reduce
            llm_note = f"LLM resize x{mult:.2f}"
        if required and not review.approved:
            volume = 0.0
        elif not required and not review.approved:
            llm_note = "advisory LLM veto ignored"

        reason_bits = [f"quant consensus {consensus.strength:+.2f}",
                       review.comment or "review ok"]
        if sizing_note:
            reason_bits.append(sizing_note)
        if llm_note:
            reason_bits.append(llm_note)
        if volume <= 0:
            reason_bits.append("no tradeable size")

        plan = TradePlan(
            symbol=symbol,
            action="buy" if consensus.direction > 0 else "sell",
            direction=consensus.direction,
            volume=volume,
            sl=sl, tp=tp, atr=atr_val, entry_hint=entry,
            consensus=consensus, review=review,
            reason=" | ".join(reason_bits),
        )
        return plan
