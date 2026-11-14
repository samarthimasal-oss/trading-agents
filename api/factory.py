"""Production service factory.

The factory is deliberately forgiving at startup: a missing MT5 terminal or
an invalid config must not prevent the dashboard from loading and explaining
what is wrong.  Individual endpoints surface a structured 503/500 instead.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from api.repository import DashboardStore
from api.services import ApiSettings, Services
from api.services.backtests_service import BacktestsService
from api.services.bridge import bridge_check
from api.services.committee_service import CommitteeService
from api.services.guardian_writer import GuardianWriter
from api.services.journal_adapter import JournalAdapter
from api.services.learning_service import LearningService
from api.services.market_service import MarketService
from api.services.mt5_adapter import MT5Adapter, UnavailableMT5
from api.services.risk_service import RiskService
from api.services.runner_service import RunnerService
from api.services.settings_service import SettingsService
from api.services.strategies_service import StrategiesService

log = logging.getLogger("api.factory")


def _read_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_services(settings: ApiSettings | None = None) -> Services:
    settings = settings or ApiSettings.load()
    config = _read_config(settings.config_path)
    store = DashboardStore(settings.db_path)
    config_service = SettingsService(settings, store)

    trading = config.get("trading") or {}
    logging_cfg = config.get("logging") or {}
    journal_path = Path(logging_cfg.get("db_file") or "state/agents.sqlite")
    if not journal_path.is_absolute():
        journal_path = settings.root / journal_path
    journal = JournalAdapter(journal_path)
    guardian = GuardianWriter(journal_path)

    mt5: Any
    try:
        from core.mt5_client import MT5Client, MT5_AVAILABLE

        if not MT5_AVAILABLE:
            raise RuntimeError("the MetaTrader5 Python package is not installed")
        mt5 = MT5Adapter(
            MT5Client(config.get("mt5") or {}),
            magic=int(trading.get("magic") or 0),
            risk_cfg=config.get("risk") or {},
            comment=str(trading.get("comment") or "TradingAgents"),
        )
    except Exception as exc:                            # noqa: BLE001
        log.warning("MT5 unavailable at startup: %s", exc)
        mt5 = UnavailableMT5(str(exc))

    runner = RunnerService(
        root=settings.root,
        pid_file=settings.root / "state" / "dashboard.json",
        log_file=settings.root / "state" / "run.log",
    )
    risk = RiskService(settings, store, mt5, journal, runner, config_service,
                       state_store=guardian)
    market = MarketService(mt5, settings.root, config_loader=config_service.raw)
    committee = CommitteeService(settings.root, config_loader=config_service.raw)
    strategies = StrategiesService(settings.root, config_service, store)
    learning = LearningService(settings.root, journal)
    backtests = BacktestsService(settings.root, store)

    return Services(
        settings=settings,
        store=store,
        mt5=mt5,
        journal=journal,
        runner=runner,
        risk=risk,
        config=config_service,
        market=market,
        strategies=strategies,
        committee=committee,
        learning=learning,
        backtests=backtests,
        bridge_check=lambda: bridge_check(settings.bridge_host, settings.bridge_port),
        closeables=[store.close, mt5.shutdown, journal.close],
    )


__all__ = ["build_services"]
