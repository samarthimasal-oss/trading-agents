"""Rollback drills: restore a config backup and a guardian-state database."""
from __future__ import annotations

import json
import shutil

from core.state_store import StateStore
from research import tune


def test_config_backup_restores_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    original = {"agents": {"consensus_threshold": 0.30}, "risk": {}}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(tune, "validate_config", lambda cfg: [])

    tune.apply_patch_file(str(path), {"agents": {"consensus_threshold": 0.40}})
    backup = tmp_path / "config.json.bak_pre_tune"
    assert backup.exists()

    shutil.copyfile(backup, path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_guardian_state_can_be_restored_from_a_database_backup(tmp_path):
    db = tmp_path / "agents.sqlite"
    store = StateStore(str(db))
    store.save_guardian_state(
        day="2026-11-28", day_start_equity=100_000.0,
        halted_day="2026-11-28", halt_reason="daily loss limit")
    store.close()

    backup = tmp_path / "agents.sqlite.bak"
    shutil.copyfile(db, backup)

    store = StateStore(str(db))
    store.save_guardian_state(
        day="2026-11-29", day_start_equity=99_000.0, halted_day=None)
    store.close()

    shutil.copyfile(backup, db)
    restored = StateStore(str(db))
    state = restored.guardian_state()
    restored.close()
    assert state["day"] == "2026-11-28"
    assert state["halted_day"] == "2026-11-28"
    assert state["halt_reason"] == "daily loss limit"
