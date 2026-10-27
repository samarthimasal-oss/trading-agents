"""Risk agent: turn a direction into a *sized, filtered, approved* trade plan.

Position sizing: risk a fixed % of equity against the ATR stop distance.
Filters: spread, position counts, trading hours, cooldown between trades.
The LLM review's size multiplier is applied here as well.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from core.models import MarketContext, AccountSnapshot, Consensus, LlmReview, TradePlan
from core.mt5_client import MT5Client

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
                      atr_val: float) -> float:
        risk_cfg = self.cfg["risk"]
        fixed = float(risk_cfg.get("fixed_lot") or 0)
        if fixed > 0:
            return self._round_volume(fixed, info)
        sl_mult = float(self.cfg["agents"]["atr_sl_mult"])
        sl_distance = max(atr_val * sl_mult, 10 * info["point"])
        ticks = sl_distance / info["trade_tick_size"]
        risk_amount = equity * float(risk_cfg["risk_percent_per_trade"]) / 100.0
        value_per_tick_per_lot = info["trade_tick_value"]
        loss_per_lot = ticks * value_per_tick_per_lot
        if loss_per_lot <= 0:
            return info["volume_min"]
        volume = risk_amount / loss_per_lot
        # notional leverage cap: never let one position control more than
        # max_notional_leverage_per_position x equity (tight ATR stops would
        # otherwise explode the volume — e.g. 13 lots on a 7-pip stop)
        lev_cap = float(risk_cfg.get("max_notional_leverage_per_position") or 0)
        if lev_cap > 0:
            mid = (ctx.bid or ctx.ask) or ((ctx.ask or ctx.bid)) or 0.0
            if ctx.bid > 0 and ctx.ask > 0:
                mid = (ctx.bid + ctx.ask) / 2.0
            notional_1lot = self._notional_usd_per_lot(ctx.symbol, info, mid)
            if equity > 0 and notional_1lot > 0:
                volume = min(volume, equity * lev_cap / notional_1lot)
        return self._round_volume(min(info["volume_max"], max(info["volume_min"], volume)), info)

    @staticmethod
    def _round_volume(volume: float, info: dict) -> float:
        step = info["volume_step"] or 0.01
        vol = int(volume / step) * step
        vol = max(info["volume_min"], min(info["volume_max"], vol))
        return round(vol, 3)

    # ------------------------------------------------------------------ gates
    def pre_trade_gate(self, ctx: MarketContext, account: AccountSnapshot,
                       open_positions: list) -> RiskGate:
        """Cheap filters that run even before consensus is computed."""
        r = self.cfg["risk"]
        # a stale/crossed broker quote (ask=0, bid=0) must never pass the gate:
        # the spread can even look negative and slip through a "> limit" check
        if ctx.bid <= 0 or ctx.ask <= 0 or ctx.spread_points < 0 or ctx.ask < ctx.bid:
            return RiskGate(False, f"invalid quote (bid {ctx.bid}, ask {ctx.ask})")
        spread_limit = float(r["max_spread_points"])
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
                   account: AccountSnapshot) -> TradePlan:
        """Compose the executable plan: entry, volume, SL, TP."""
        symbol = ctx.symbol
        info = MT5Client.symbol_settings(self.client.prepare(symbol))
        atr_val = ctx.atr or 0.0
        if atr_val <= 0:
            return TradePlan(symbol, "none", reason="ATR unavailable")

        sl_mult = float(agent_cfg["atr_sl_mult"])
        tp_mult = float(agent_cfg["atr_tp_mult"])
        stop_dist = max(atr_val * sl_mult, info["stops_level"] * info["point"] * 2.0)
        tp_dist = atr_val * tp_mult
        tick = self.client.tick(symbol)
        if consensus.direction > 0:
            entry, sl, tp = tick.ask, tick.ask - stop_dist, tick.ask + tp_dist
        else:
            entry, sl, tp = tick.bid, tick.bid + stop_dist, tick.bid - tp_dist
        entry, sl, tp = (
            round(entry, info["digits"]), round(sl, info["digits"]), round(tp, info["digits"]))

        volume = self.size_position(ctx, info, account.equity, atr_val)
        if review.action == "resize":
            volume = self._round_volume(max(info["volume_min"], volume *
                                            min(max(review.size_multiplier, 0.0), 1.0)), info)
        volume = round(volume * (1.0 if review.approved else 0.0), 3)

        plan = TradePlan(
            symbol=symbol,
            action="buy" if consensus.direction > 0 else "sell",
            direction=consensus.direction,
            volume=volume,
            sl=sl, tp=tp, atr=atr_val, entry_hint=entry,
            consensus=consensus, review=review,
            reason="quant consensus %+.2f | %s" % (consensus.strength,
                                                   review.comment or "review ok"),
        )
        return plan
