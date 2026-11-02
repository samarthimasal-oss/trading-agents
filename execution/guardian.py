"""Guardian: the supervisor agent that protects the ACCOUNT.

It can override every other agent:
  - max drawdown from the historical equity peak  -> flatten + lockout
  - daily realized+floating loss limit            -> flatten + lockout until next day
  - floors: equity <= 0                           -> flatten + hard stop

The lockout and the day-start equity are persisted in SQLite.  A restart must
not clear a risk halt: otherwise Stop/Start on the dashboard would silently
reset the daily loss budget.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("guardian")


class Guardian:
    def __init__(self, cfg: dict, client, store):
        self.risk_cfg = cfg["risk"]
        self.client = client
        self.store = store
        self.magic = None
        self._halted_day: str | None = None
        self._halt_reason: str = ""
        self._halted_at: float | None = None
        self._last_day: str | None = None
        self.day_start_equity: float | None = None
        self._load_state()

    def utc_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ------------------------------------------------------------- persistence
    def _load_state(self) -> None:
        if not self.store:
            return
        try:
            state = self.store.guardian_state()
        except Exception as exc:                      # noqa: BLE001
            log.warning("guardian state unreadable (starting fresh): %s", exc)
            return
        if not state:
            return
        self._last_day = state.get("day")
        self.day_start_equity = state.get("day_start_equity")
        self._halted_day = state.get("halted_day")
        self._halt_reason = state.get("halt_reason") or ""
        self._halted_at = state.get("halted_at")
        if self._halted_day:
            log.warning("Guardian restored lockout for %s: %s",
                        self._halted_day, self._halt_reason or "risk limit")

    def _persist(self) -> None:
        if not self.store:
            return
        try:
            self.store.save_guardian_state(
                day=self._last_day or self.utc_day(),
                day_start_equity=self.day_start_equity,
                halted_day=self._halted_day,
                halt_reason=self._halt_reason,
                halted_at=self._halted_at,
            )
        except Exception as exc:                      # noqa: BLE001
            log.error("could not persist guardian state: %s", exc)

    # ------------------------------------------------------------------- halt
    def is_halted(self) -> tuple[bool, str]:
        if self._halted_day == self.utc_day():
            reason = self._halt_reason or "guardian lockout (risk limit hit today)"
            return True, reason
        return False, ""

    def _lockout_until_tomorrow(self, why: str) -> None:
        self._halted_day = self.utc_day()
        self._halt_reason = why
        self._halted_at = time.time()
        log.error("GUARDIAN LOCKOUT: %s", why)
        if self.store:
            self.store.event(kind="LOCKOUT", detail=why)
        self._persist()

    # ------------------------------------------------------------ risk checks
    def check(self) -> tuple[bool, str, bool]:
        """Returns (trading_allowed, reason, flatten_requested)."""
        from core.mt5_client import MT5Error

        try:
            acc = self.client.account()
        except MT5Error as e:
            return True, f"account snapshot unavailable ({e})", False

        # roll the day and expire yesterday's lockout
        today = self.utc_day()
        if self._last_day != today:
            self._last_day = today
            self.day_start_equity = acc.equity
            if self._halted_day and self._halted_day != today:
                log.info("Guardian lockout for %s expired at the new UTC day",
                         self._halted_day)
                self._halted_day = None
                self._halt_reason = ""
                self._halted_at = None
            self._persist()

        # a persisted halt is authoritative for the whole day
        halted, why = self.is_halted()
        if halted:
            return False, why, True

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

        # no magic configured: account-level checks only
        return True, "account checks passed", False

    def flatten(self, execute) -> int:
        try:
            return execute.close_all()
        except Exception as exc:
            log.error("flatten failed: %s", exc)
            return 0

    def reset(self) -> None:
        self._halted_day = None
        self._halt_reason = ""
        self._halted_at = None
        if self.store:
            self.store.event(kind="GUARDIAN_RESET", detail="manual reset")
        self._persist()
        log.info("Guardian manually reset")
