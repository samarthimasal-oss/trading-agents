"""Secret-audit helpers: pattern detection and tracked-file hygiene."""
from __future__ import annotations

from scripts.audit_secrets import (
    local_logs, scan_diff, scan_text, sensitive_tracked,
)


def test_scan_text_flags_patterns_without_values():
    token = "AIza" + "A" * 35
    assert scan_text(f"request failed for {token}") == ["google_api_key"]
    assert scan_text("no credentials here") == []


def test_local_logs_skips_dependencies(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "run.log").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.log").write_text("x", encoding="utf-8")
    assert local_logs(str(tmp_path)) == [str(tmp_path / "state" / "run.log")]


def test_scan_diff_flags_credentials_without_echoing_them():
    token = "AIza" + "A" * 35
    text = f"commit abcdef123456\n+key = '{token}'\n"
    hits = scan_diff(text)
    assert hits == [{"commit": "abcdef123456", "pattern": "google_api_key"}]
    assert token not in repr(hits)


def test_scan_diff_ignores_removed_lines_and_file_headers():
    token = "AIza" + "A" * 35
    text = (f"commit abcdef123456\n-key = '{token}'\n"
            "+++ b/config.json\n")
    assert scan_diff(text) == []


def test_sensitive_tracked_flags_config_state_and_backups():
    paths = [
        "README.md",
        "config.json",
        "config.json.bak_pre_tune",
        ".env.local",
        "state/agents.sqlite",
        "backups/2026-11-02/config.json",
        "web/dist/app.js",
    ]
    assert sensitive_tracked(paths) == [
        ".env.local",
        "backups/2026-11-02/config.json",
        "config.json",
        "config.json.bak_pre_tune",
        "state/agents.sqlite",
    ]
