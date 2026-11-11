"""Configuration validation must fail fast with every problem listed."""
from __future__ import annotations

import copy
import json
import os

import pytest

from core.config_schema import ConfigError, validate_config


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _example() -> dict:
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_example_config_is_valid():
    cfg = validate_config(_example())
    assert cfg["trading"]["symbol"]
    assert cfg["trading"]["timeframe"] in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}


def test_negative_risk_is_rejected():
    cfg = copy.deepcopy(_example())
    cfg["risk"]["risk_percent_per_trade"] = -1
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "risk_percent_per_trade" in str(exc.value)


def test_empty_symbols_are_rejected():
    cfg = copy.deepcopy(_example())
    cfg["trading"]["symbols"] = []
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "symbols" in str(exc.value)


def test_bad_timeframe_is_rejected():
    cfg = copy.deepcopy(_example())
    cfg["trading"]["timeframe"] = "M7"
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "timeframe" in str(exc.value)


def test_missing_section_is_reported():
    cfg = copy.deepcopy(_example())
    del cfg["risk"]
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "risk" in str(exc.value)


def test_per_symbol_spread_map_must_be_numeric():
    cfg = copy.deepcopy(_example())
    cfg["risk"]["max_spread_points_by_symbol"] = {"XAUUSD": "wide"}
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "max_spread_points_by_symbol" in str(exc.value)


def test_per_symbol_spread_map_accepts_numbers():
    cfg = copy.deepcopy(_example())
    cfg["risk"]["max_spread_points_by_symbol"] = {"XAUUSD": 80}
    validated = validate_config(cfg)
    assert validated["risk"]["max_spread_points_by_symbol"]["XAUUSD"] == 80


def test_unknown_strategy_engine_is_rejected():
    cfg = copy.deepcopy(_example())
    cfg["strategies"]["engines"]["unknown"] = {"enabled": True}
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "unknown strategy" in str(exc.value)


def test_strategy_confidence_must_be_a_probability():
    cfg = copy.deepcopy(_example())
    cfg["strategies"]["min_confidence"] = 2.0
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "min_confidence" in str(exc.value)


def test_ea_section_accepts_loopback_defaults():
    cfg = validate_config(copy.deepcopy(_example()))
    assert cfg["ea"]["host"] in {"127.0.0.1", "localhost", "::1"}
    assert cfg["ea"]["port"] == 8765


def test_ea_rejects_non_loopback_host():
    cfg = copy.deepcopy(_example())
    cfg["ea"]["host"] = "0.0.0.0"
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "loopback" in str(exc.value)


def test_ea_rejects_invalid_port():
    cfg = copy.deepcopy(_example())
    cfg["ea"]["port"] = 70000
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "ea.port" in str(exc.value)


def test_llm_timeout_seconds_is_validated():
    cfg = copy.deepcopy(_example())
    cfg["llm"]["timeout_seconds"] = 0
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert "timeout_seconds" in str(exc.value)
