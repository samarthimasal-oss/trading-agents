"""API settings and the injectable service container.

Tests build a ``Services`` object with fakes; production builds the same shape
from ``config.json``, MT5 and the dashboard's SQLite store.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[2]


def ensure_dashboard_token(path: Path) -> str:
    """Read the local dashboard token, creating it on first run."""
    path = Path(path)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return value


@dataclass(frozen=True)
class ApiSettings:
    root: Path
    config_path: Path
    db_path: Path
    token_file: Path
    dashboard_token: str
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    log_paths: tuple[Path, ...] = ()
    static_dir: Optional[Path] = None
    ws_interval: float = 2.0

    @classmethod
    def load(cls, root: str | Path | None = None,
             config_path: str | Path | None = None) -> "ApiSettings":
        root = Path(root or ROOT)
        config_path = Path(config_path or root / "config.json")
        config: dict = {}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        logging_cfg = config.get("logging") or {}
        ea = config.get("ea") or {}
        state = root / "state"
        token_file = state / "dashboard_token.txt"
        logs = [state / "run.log", state / "ea_bridge.err.log"]
        if logging_cfg.get("log_file"):
            logs.insert(0, root / str(logging_cfg["log_file"]))
        static_dir = root / "web" / "dist"
        return cls(
            root=root,
            config_path=config_path,
            db_path=state / "dashboard.sqlite",
            token_file=token_file,
            dashboard_token=ensure_dashboard_token(token_file),
            bridge_host=str(ea.get("host") or "127.0.0.1"),
            bridge_port=int(ea.get("port") or 8765),
            log_paths=tuple(dict.fromkeys(Path(p) for p in logs)),
            static_dir=static_dir if static_dir.exists() else None,
        )


@dataclass
class Services:
    settings: ApiSettings
    store: Any
    mt5: Any
    journal: Any
    runner: Any
    risk: Any
    config: Any
    market: Any
    strategies: Any
    committee: Any
    learning: Any
    backtests: Any
    bridge_check: Callable[[], dict]
    clock: Callable[[], float] = time.time
    started_at: float = field(default_factory=time.time)
    closeables: list[Callable[[], None]] = field(default_factory=list)


__all__ = ["ApiSettings", "Services", "ensure_dashboard_token"]
