"""Guardian: the supervisor agent that protects the ACCOUNT.
It can override every other agent:
  - max drawdown from historical equity peak  -> flatten + lockout
  - daily realized+floating loss limit        -> flatten + lockout until next day
  - floors: equity <= 0                       -> flatten + hard stop
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("guardian")


class Guardian:
    def __init__(self, cfg: dict, client, store):
        self.risk_cfg = cfg["risk"]
        self.client = client
        self.store = store
        self.magic = None
        self._halted_day: str | None = None
        self.day_start_equity: float | None = None

    def utc_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------- halt
    def is_halted(self) -> tuple[bool, str]:
        if self._halted_day == self.utc_day():
            return True, "guardian lockout (risk limit hit today)"
        return False, ""

    def _lockout_until_tomorrow(self, why: str) -> None:
        self._halted_day = self.utc_day()
        log.error("GUARDIAN LOCKOUT: %s", why)
        if self.store:
            self.store.event(kind="LOCKOUT", detail=why)

    # ------------------------------------------------------------ risk checks
    def check(self) -> tuple[bool, str, bool]:
        """Returns (trading_allowed, reason, flatten_requested)."""

        from core.mt5_client import MT5Error
        try:
            acc = self.client.account()
        except MT5Error as e:
            return True, f"account snapshot unavailable ({e})", False

        # track day-start equity for the daily loss metric
        today = self.utc_day()
        if getattr(self, "_last_day", None) != today:
            self._last_day = today
            self.day_start_equity = acc.equity
        day_start = self.day_start_equity or acc.equity

        # 1) hard equity floor
        if acc.equity <= 0:
            self._lockout_until_tomorrow("equity <= 0")
            return False, "equity exhausted", True

        # 2) historical peak drawdown (persisted across restarts in DB)
        peak = self.store.peak_equity() if self.store else 0.0
        peak = max(peak, acc.equity)
        max_dd = float(self.risk_cfg["max_equity_drawdown_percent"]) / 100.0
        if self.store:
            self.store.equity(acc.equity, acc.balance, acc.margin, acc.profit)
        if peak > 0 and acc.equity < peak * (1 - max_dd):
            why = (f"equity {acc.equity:.2f} dropped >"
                   f"{max_dd:.0%} from peak {peak:.2f}")
            self._lockout_until_tomorrow(why)
            return False, why, True

        # 3) daily loss limit (bot trades: realized today + floating)
        if self.magic:
            daily = self.client.daily_bot_profit(self.magic)
            limit = float(self.risk_cfg["max_daily_loss_percent"]) / 100.0 * day_start
            if daily <= -limit:
                why = (f"today's bot P/L {daily:.2f} breaches daily limit "
                       f"{-limit:.2f} (risk {self.risk_cfg['max_daily_loss_percent']}%)")
                self._lockout_until_tomorrow(why)
                return False, why, True
            return True, f"daily bot P/L {daily:+.2f}", False

    def flatten(self, execute) -> int:
        try:
            return execute.close_all()
        except Exception as exc:
            log.error("flatten failed: %s", exc)
            return 0

    def reset(self) -> None:
        self._halted_day = None
        log.info("Guardian manually reset")
