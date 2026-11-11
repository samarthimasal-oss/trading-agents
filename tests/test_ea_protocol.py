"""Protocol contract tests: auth, envelopes, intents, acks and token storage."""
from __future__ import annotations

import json
import os
import time

import pytest

from execution.ea_protocol import (
    Ack,
    AuthenticationError,
    ClockSkewError,
    Envelope,
    Intent,
    IntentError,
    MAX_FRAME_BYTES,
    ProtocolError,
    create_token,
    idempotency_key,
    load_token,
    make_envelope,
    redact,
    token_matches,
)


TOKEN = "a" * 64


def _envelope(msg_type: str = "heartbeat", payload: dict | None = None,
              seq: int = 1, ts: float | None = None) -> Envelope:
    return make_envelope(msg_type, payload or {"ok": True}, TOKEN, seq,
                         msg_id=f"test-{seq}", ts=time.time() if ts is None else ts)


def test_envelope_roundtrip():
    envelope = _envelope("intent", {"symbol": "XAUUSD", "direction": 1})
    decoded = Envelope.from_line(envelope.to_bytes(), TOKEN)
    assert decoded.type == "intent"
    assert decoded.payload["symbol"] == "XAUUSD"
    assert decoded.seq == 1
    assert decoded.id == "test-1"
    assert envelope.token not in repr(envelope)


def test_bad_token_is_rejected():
    envelope = _envelope()
    body = json.loads(envelope.to_json())
    body["token"] = "b" * 64
    with pytest.raises(AuthenticationError):
        Envelope.from_line(json.dumps(body), TOKEN)


def test_clock_skew_is_rejected():
    envelope = _envelope(ts=time.time() - 30)
    with pytest.raises(ClockSkewError):
        Envelope.from_line(envelope.to_json(), TOKEN, now=time.time(), max_skew=10)


def test_version_and_shape_validation():
    envelope = _envelope()
    body = json.loads(envelope.to_json())
    body["v"] = 2
    with pytest.raises(ProtocolError):
        Envelope.from_line(json.dumps(body), TOKEN)
    body["v"] = 1
    body["seq"] = 0
    with pytest.raises(ProtocolError):
        Envelope.from_line(json.dumps(body), TOKEN)
    body["seq"] = 1
    body["payload"] = []
    with pytest.raises(ProtocolError):
        Envelope.from_line(json.dumps(body), TOKEN)


def test_oversized_frame_is_rejected():
    with pytest.raises(ProtocolError):
        Envelope.from_line(b"{" + b"x" * (MAX_FRAME_BYTES + 1), TOKEN)


def test_idempotency_key_is_deterministic_and_direction_sensitive():
    a = idempotency_key("trend", "XAUUSD", 1_793_633_400, 1)
    b = idempotency_key("trend", "XAUUSD", 1_793_633_400.9, 1)
    c = idempotency_key("trend", "XAUUSD", 1_793_633_400, -1)
    assert a == b
    assert a != c
    assert len(a) == 64


def test_intent_validation_and_payload_roundtrip():
    intent = Intent(
        idempotency_key=idempotency_key("trend", "XAUUSD", 1_793_633_400, 1),
        symbol="XAUUSD", direction=1, volume=0.1,
        sl=3935.2, tp=3975.4, strategy="trend",
    )
    payload = intent.to_payload()
    restored = Intent.from_payload(payload)
    assert restored.to_payload() == payload
    assert payload["comment"] == "TA:trend"


def test_intent_rejects_inverted_stops_and_unknown_strategy():
    with pytest.raises(IntentError):
        Intent("k" * 16, "XAUUSD", 1, 0.1, 4000.0, 3900.0, "trend").validate()
    with pytest.raises(IntentError):
        Intent("k" * 16, "XAUUSD", 1, 0.1, 3900.0, 4000.0, "martingale").validate()
    with pytest.raises(IntentError):
        Intent.from_payload({"idempotency_key": "k" * 16, "symbol": "XAUUSD"})


def test_ack_parsing():
    ack = Ack.from_payload({
        "idempotency_key": "k" * 16,
        "status": "filled",
        "ticket": 123,
        "price": 3948.1,
        "volume": 0.1,
        "retcode": 10009,
        "retcode_name": "TRADE_RETCODE_DONE",
    })
    assert ack.filled and ack.ticket == 123
    assert ack.terminal_state
    with pytest.raises(ProtocolError):
        Ack.from_payload({"idempotency_key": "k", "status": "maybe"})


def test_token_creation_and_loading(tmp_path):
    path = tmp_path / "ea_token.txt"
    value, target = create_token(str(path))
    assert len(value) == 64
    assert str(target) == str(path)
    assert path.read_text(encoding="utf-8").strip() == value
    assert load_token(str(path)) == value
    assert token_matches(value, value)
    assert not token_matches(value, "x" * 64)
    assert redact(f"token={value}", value) == "token=<redacted>"
