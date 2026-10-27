"""Thin, defensive wrapper around the MetaTrader5 Python API.

If the MetaTrader5 package or the terminal is not installed yet, the app still
runs in "report" mode and tells you exactly what is missing (see main.py check).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False

from core.models import Candle, MarketContext, AccountSnapshot

log = logging.getLogger("mt5_client")


class MT5Error(RuntimeError):
    pass


TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}


class MT5Client:
    def __init__(self, cfg: dict):
        if not MT5_AVAILABLE:
            raise MT5Error(
                "The MetaTrader5 python package is not installed. Run: pip install MetaTrader5"
            )
        self.cfg = cfg
        self.connected = False

    # ------------------------------------------------------------------ setup
    def connect(self) -> bool:
        path = (self.cfg.get("terminal_path") or "").strip()
        login = int(self.cfg.get("login") or 0)
        password = self.cfg.get("password") or ""
        server = self.cfg.get("server") or ""

        kwargs: dict = {"timeout": int(self.cfg.get("timeout_ms") or 60000)}
        if path:
            kwargs["path"] = path
        if login and password and server:
            kwargs.update({"login": login, "password": password, "server": server})

        if not mt5.initialize(**kwargs):
            failed = mt5.last_error()
            hint = (
                " Is MetaTrader 5 running (and logged in on the terminal)? "
                "Or set mt5.terminal_path in config.json."
                if not path else " Check the terminal_path in config.json."
            )
            raise MT5Error(f"MT5 initialize failed: {failed}.{hint}")

        self.connected = True
        acc = mt5.account_info()
        log.info(
            "Connected to MT5: login=%s server=%s terminal=%s",
            acc.login if acc else "?",
            acc.server if acc else "?",
            mt5.version(),
        )
        return True

    def shutdown(self):
        try:
            mt5.shutdown()
        finally:
            self.connected = False

    def ensure(self):
        if not self.connected:
            self.connect()

    # ------------------------------------------------------------- symbol data
    def resolve_symbol(self, symbol: str) -> str:
        """Return the broker's exact symbol name (fuzzy match)."""
        self.ensure()
        if mt5.symbol_info(symbol):
            return symbol
        wanted = symbol.upper().replace("/", "")
        for info in mt5.symbols_get():
            if info.name.upper().replace("/", "").replace("/", "") == wanted:
                mt5.symbol_select(info.name, True)
                return info.name
        # try common suffixes via substring
        for info in mt5.symbols_get():
            if wanted in info.name.upper().replace("/", ""):
                mt5.symbol_select(info.name, True)
                return info.name
        raise MT5Error(f"Symbol {symbol!r} not found in broker's symbol list")

    def prepare(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise MT5Error(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        return info

    def copy_bars(self, symbol: str, tf_label: str, count: int) -> list[Candle]:
        self.ensure()
        tf = getattr(mt5, f"TIMEFRAME_{tf_label}", mt5.TIMEFRAME_M15)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise MT5Error(f"copy_rates failed for {symbol}: {mt5.last_error()}")
        return [
            Candle(r["time"], r["open"], r["high"], r["low"], r["close"], r["tick_volume"])
            for r in rates
        ]

    def build_context(self, symbol: str, tf_label: str, count: int) -> MarketContext:
        info = self.prepare(symbol)
        ind = mt5.symbol_info_tick(symbol)
        if ind is None:
            raise MT5Error(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
        return MarketContext(
            symbol=symbol,
            timeframe=tf_label,
            digits=info.digits,
            point=info.point,
            bid=ind.bid,
            ask=ind.ask,
            spread_points=(ind.ask - ind.bid) / info.point if ind.bid and info.point else 0.0,
        )

    # -------------------------------------------------------------- account
    def account(self) -> AccountSnapshot:
        self.ensure()
        a = mt5.account_info()
        if a is None:
            raise MT5Error(f"account_info failed: {mt5.last_error()}")
        return AccountSnapshot(
            login=a.login, balance=a.balance, equity=a.equity, margin=a.margin,
            free_margin=a.margin_free, margin_level=a.margin_level, profit=a.profit,
            currency=a.currency,
        )

    def daily_bot_profit(self, magic: int) -> float:
        """Realized P/L of the bot's closed deals today (UTC) + current floating P/L."""
        self.ensure()
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(start, datetime.now(timezone.utc)) or []
        profit = 0.0
        for d in deals:
            if d.magic != magic:
                continue
            if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                profit += d.profit + d.swap + d.commission
        profit += self.account().profit          # floating P/L right now
        return profit

    # -------------------------------------------------------------- position helpers
    def positions(self, symbol: str = None, magic: int = None) -> list:
        self.ensure()
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []
        if magic is not None:
            positions = [p for p in positions if p.magic == magic]
        return list(positions)

    def orders(self, symbol: str = None, magic: int = None) -> list:
        self.ensure()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if orders is None:
            return []
        if magic is not None:
            orders = [o for o in orders if o.magic == magic]
        return list(orders)

    # -------------------------------------------------------------- order engine
    @staticmethod
    def _pick_filling(info) -> int:
        """Choose a filling mode the symbol actually allows.

        Empirically verified on MetaQuotes-Demo (order_check retcodes):
        symbol_info().filling_mode is a bitmask — 1 = FOK, 2 = IOC.
        EURUSD (flag=1) accepts only FOK; XAUUSD (flag=3) accepts FOK+IOC.
        """
        allowed = int(getattr(info, "filling_mode", 0) or 0)
        if allowed & 1:
            return mt5.ORDER_FILLING_FOK
        if allowed & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    # retcodes that mean "do not bother sending, it cannot fill now"
    _HARD_FAIL = frozenset(
        r for r in (
            getattr(mt5, "TRADE_RETCODE_NO_MONEY", None),
            getattr(mt5, "TRADE_RETCODE_INVALID_VOLUME", None),
            getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", None),
            getattr(mt5, "TRADE_RETCODE_POSITION_CLOSED", None),
            getattr(mt5, "TRADE_RETCODE_INVALID", None),
        ) if r is not None
    )
    _RETRY_CODES = frozenset(
        r for r in (
            getattr(mt5, "TRADE_RETCODE_INVALID_FILL", None),
            getattr(mt5, "TRADE_RETCODE_REQUOTE", None),
            getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None),
            getattr(mt5, "TRADE_RETCODE_PRICE_OFF", None),
        ) if r is not None
    )

    def send_order(self, request: dict, max_retries: int = 3) -> dict:
        """Send an order with retcode-aware retries (requote / filling mode)."""
        self.ensure()
        last = None
        for attempt in range(max_retries):
            check = mt5.order_check(request)
            if check is None:
                raise MT5Error(f"order_check failed: {mt5.last_error()}")
            if check.retcode in self._HARD_FAIL:
                return {"retcode": check.retcode, "checked": check}
            result = mt5.order_send(request)
            if result is None:
                raise MT5Error(f"order_send failed: {mt5.last_error()}")
            if result.retcode in (
                mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL, mt5.TRADE_RETCODE_PLACED
            ):
                return {"retcode": result.retcode, "deal": result.deal,
                        "order": result.order,
                        "price": result.price or 0.0, "volume": result.volume or request["volume"],
                        "checked": check}
            last = result
            if result.retcode in self._RETRY_CODES:
                time.sleep(0.4 * (attempt + 1))
                request["deviation"] = max(int(request.get("deviation", 10)), 10 * (attempt + 1))
                continue
            break
        return {"retcode": last.retcode if last else -1}

    # -------------------------------------------------------------- static info
    def tick(self, symbol: str):
        self.ensure()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            raise MT5Error(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
        return t

    @staticmethod
    def symbol_settings(info) -> dict:
        return {
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step or 0.01,
            "trade_contract_size": float(getattr(info, "trade_contract_size", 0.0) or 0.0),
            "trade_tick_value": info.trade_tick_value or 1.0,
            "trade_tick_size": info.trade_tick_size or info.point,
            "point": info.point,
            "digits": info.digits,
            "stops_level": max(int(round(abs(info.trade_stops_level))), 0),
            "freezing_level": max(int(round(abs(info.trade_freeze_level))), 0),
            "trade_mode": info.trade_mode,
        }
