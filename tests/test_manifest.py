"""Reproducibility manifest: stable hashes, no secrets, no local paths."""
from __future__ import annotations

import json

import pandas as pd

from research import manifest


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 00:15"]),
        "open": [1.0, 1.1],
        "high": [1.2, 1.3],
        "low": [0.9, 1.0],
        "close": [1.1, 1.2],
        "spread": [8.0, 9.0],
        "tick_volume": [10, 11],
    })


def test_data_fingerprint_ignores_order_and_extra_columns():
    frame = _frame()
    base = manifest.data_fingerprint(frame)
    assert manifest.data_fingerprint(
        frame[list(reversed(frame.columns))]) == base
    assert manifest.data_fingerprint(
        frame.assign(unrelated="not replayed")) == base


def test_data_fingerprint_changes_with_price_or_time():
    frame = _frame()
    base = manifest.data_fingerprint(frame)

    changed = frame.copy()
    changed.loc[0, "close"] = 1.1000001
    assert manifest.data_fingerprint(changed) != base

    shifted = frame.copy()
    shifted.loc[0, "time"] = pd.Timestamp("2026-01-01 00:01")
    assert manifest.data_fingerprint(shifted) != base


def test_sha256_value_is_key_order_independent():
    assert manifest.sha256_value({"a": 1, "b": [2, 3]}) == (
        manifest.sha256_value({"b": [2, 3], "a": 1}))
    assert manifest.sha256_value({"a": 1}) != manifest.sha256_value({"a": 2})


def test_data_summary_records_range_and_relative_source(tmp_path):
    path = tmp_path / "bars.csv"
    _frame().to_csv(path, index=False)
    summary = manifest.data_summary(
        _frame(), source_path=str(path), root=str(tmp_path))
    assert summary["bars"] == 2
    assert summary["first"].startswith("2026-01-01T00:00")
    assert summary["last"].startswith("2026-01-01T00:15")
    assert summary["source"] == "bars.csv"
    assert summary["source_sha256"] == manifest.sha256_file(str(path))
    assert str(tmp_path) not in json.dumps(summary)


def test_file_summary_missing_file_is_explicit(tmp_path):
    summary = manifest.file_summary(
        str(tmp_path / "missing.pkl"), root=str(tmp_path))
    assert summary["exists"] is False
    assert summary["sha256"] is None
    assert summary["source"] == "missing.pkl"


def test_build_manifest_hashes_config_without_writing_secrets(tmp_path):
    frame = _frame()
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    config = {"trading": {"symbol": "XAUUSD"},
              "llm": {"api_key": "SECRET-VALUE"}}
    inputs = {"bars_copy": manifest.file_summary(str(path), root=str(tmp_path))}

    built = manifest.build_manifest(
        timeframe="M15", symbols=["XAUUSD"],
        data={"XAUUSD": manifest.data_summary(
            frame, source_path=str(path), root=str(tmp_path))},
        config=config, root=str(tmp_path),
        costs={"commission_per_lot_round_turn": 7.0},
        split={"train": [0, 10]},
        parameters={"days": 30}, inputs=inputs)

    text = json.dumps(built)
    assert "SECRET-VALUE" not in text
    assert str(tmp_path) not in text
    assert built["config"]["sha256"] == manifest.sha256_value(config)
    assert built["config"]["top_level_keys"] == ["llm", "trading"]
    assert built["costs_sha256"] == manifest.sha256_value(
        {"commission_per_lot_round_turn": 7.0})
    assert built["split"] == {"train": [0, 10]}
    assert built["data"]["XAUUSD"]["sha256"] == manifest.data_fingerprint(frame)
    assert built["data"]["XAUUSD"]["source"] == "bars.csv"
    assert built["inputs"]["bars_copy"]["sha256"] == manifest.sha256_file(
        str(path))
    assert built["code"]["source_sha256"]
    assert built["environment"]["python"]
