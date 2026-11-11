"""Loopback bridge tests using a fake EA socket, no MetaTrader required."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from execution.ea_client import (
    DuplicateIntentError,
    EAOrderBridge,
    EATimeout,
)
from execution.ea_protocol import (
    Envelope,
    Intent,
    idempotency_key,
    make_envelope,
)


TOKEN = "b" * 64


class FakeEA:
    def __init__(self, host: str, port: int, token: str = TOKEN):
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(5.0)
        self.reader = self.sock.makefile("rb")
        self.token = token
        self.seq = 0
        self.send("hello", {
            "role": "ea", "version": "1.0.0", "account": 5057915388,
            "symbol": "XAUUSD", "mode": "TESTER",
        })

    def send(self, msg_type: str, payload: dict) -> None:
        self.seq += 1
        envelope = make_envelope(msg_type, payload, self.token, self.seq)
        self.sock.sendall(envelope.to_bytes())

    def read(self) -> Envelope:
        line = self.reader.readline()
        if not line:
            raise EOFError("bridge closed the connection")
        return Envelope.from_line(line, self.token)

    def expect(self, msg_type: str, timeout: float = 4.0) -> Envelope:
        self.sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                envelope = self.read()
            except socket.timeout:
                continue
            if envelope.type == msg_type:
                return envelope
            if envelope.type in ("heartbeat", "position_update", "hello"):
                continue
        raise AssertionError(f"did not receive {msg_type}")

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.sock.close()


@pytest.fixture
def bridge():
    instance = EAOrderBridge(TOKEN, host="127.0.0.1", port=0,
                             ack_timeout=1.0, heartbeat_timeout=2.0)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def ea(bridge):
    client = FakeEA("127.0.0.1", bridge.bound_port)
    assert bridge.wait_connected(2.0)
    # consume the server hello
    hello = client.expect("hello")
    assert hello.payload["role"] == "server"
    yield client
    client.close()


def _intent(**overrides) -> Intent:
    values = dict(
        idempotency_key=idempotency_key("trend", "XAUUSD", 1_793_633_400, 1),
        symbol="XAUUSD", direction=1, volume=0.10,
        sl=3935.20, tp=3975.40, strategy="trend",
    )
    values.update(overrides)
    return Intent(**values)


def test_handshake_reports_peer_and_hello(bridge, ea):
    assert bridge.connected
    assert bridge.hello["account"] == 5057915388
    status = bridge.status()
    assert status["authenticated"] and status["positions"] == 0


def test_send_intent_receives_ack_and_caches_duplicate(bridge, ea):
    sent = {}

    def respond():
        envelope = ea.expect("intent")
        sent.update(envelope.payload)
        ea.send("ack", {
            "idempotency_key": envelope.payload["idempotency_key"],
            "status": "filled", "ticket": 987654, "price": 3948.10,
            "volume": 0.10, "retcode": 10009,
            "retcode_name": "TRADE_RETCODE_DONE",
        })

    thread = threading.Thread(target=respond)
    thread.start()
    ack = bridge.send_intent(_intent())
    thread.join(timeout=2.0)
    assert ack is not None and ack.filled
    assert ack.ticket == 987654
    assert sent["symbol"] == "XAUUSD"
    # The same key is answered from cache without a second EA round-trip.
    again = bridge.send_intent(_intent())
    assert again is not None and again.ticket == 987654


def test_ack_timeout_is_unknown_and_blocks_retry(bridge, ea):
    errors = {}

    def send():
        try:
            bridge.send_intent(_intent(), timeout=0.2)
        except Exception as exc:  # captured for the assertion below
            errors["error"] = exc

    thread = threading.Thread(target=send)
    thread.start()
    ea.expect("intent")
    thread.join(timeout=2.0)
    assert isinstance(errors.get("error"), EATimeout)
    key = _intent().idempotency_key
    assert key in bridge.unknown_keys()
    with pytest.raises(DuplicateIntentError):
        bridge.send_intent(_intent())


def test_position_update_callback(bridge, ea):
    seen = []
    bridge.on_positions = lambda positions: seen.extend(positions)
    ea.send("position_update", {"positions": [{
        "ticket": 123456, "symbol": "XAUUSD", "direction": 1,
        "volume": 0.10, "entry": 3948.10, "sl": 3935.20, "tp": 3975.40,
        "price": 3952.00, "profit": 39.0, "swap": 0.0,
        "opened_at": 1793633404, "strategy": "trend",
    }]})
    deadline = time.time() + 2.0
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert seen and seen[0]["ticket"] == 123456
    assert bridge.positions()[0]["strategy"] == "trend"


def test_reconcile_roundtrip(bridge, ea):
    def respond():
        envelope = ea.expect("reconcile_request")
        ea.send("reconcile_result", {
            "broker_positions": [{"ticket": 123456, "volume": 0.10}],
            "broker_orders": [],
            "account": {"equity": 100000.0},
            "mismatches": [],
            "unknown_keys": envelope.payload["pending_keys"],
            "server_time": time.time(),
        })

    thread = threading.Thread(target=respond)
    thread.start()
    result = bridge.reconcile(timeout=2.0)
    thread.join(timeout=2.0)
    assert result["broker_positions"][0]["ticket"] == 123456
    assert result["mismatches"] == []


def test_close_position_ack(bridge, ea):
    def respond():
        envelope = ea.expect("close_request")
        ea.send("ack", {
            "idempotency_key": envelope.payload["idempotency_key"],
            "status": "filled", "ticket": 123456, "retcode": 10009,
            "retcode_name": "TRADE_RETCODE_DONE",
        })

    thread = threading.Thread(target=respond)
    thread.start()
    ack = bridge.close_position(123456, "committee exit")
    thread.join(timeout=2.0)
    assert ack.filled and ack.ticket == 123456


def test_rejected_ack_is_cached_and_not_retried(bridge, ea):
    def respond():
        envelope = ea.expect("intent")
        ea.send("ack", {
            "idempotency_key": envelope.payload["idempotency_key"],
            "status": "rejected", "retcode": 10016,
            "retcode_name": "TRADE_RETCODE_INVALID_STOPS",
            "message": "stop too close",
        })

    thread = threading.Thread(target=respond)
    thread.start()
    ack = bridge.send_intent(_intent())
    thread.join(timeout=2.0)
    assert ack is not None and ack.status == "rejected"
    with pytest.raises(DuplicateIntentError):
        bridge.send_intent(_intent())


def test_sequence_gap_requests_resync(bridge, ea):
    # Jump forward without sending seq 2.
    ea.seq = 4
    ea.send("heartbeat", {"equity": 100000.0})
    resync = ea.expect("resync")
    assert resync.payload["received_seq"] == 5
    assert resync.payload["expected_seq"] == 2


def test_bad_token_connection_is_closed(bridge):
    sock = socket.create_connection(("127.0.0.1", bridge.bound_port), timeout=3.0)
    bad = make_envelope("hello", {"role": "ea"}, "c" * 64, 1)
    sock.sendall(bad.to_bytes())
    sock.settimeout(3.0)
    try:
        assert sock.recv(4096) == b""
    finally:
        sock.close()
    assert not bridge.connected


def test_heartbeat_staleness_marks_disconnected():
    instance = EAOrderBridge(TOKEN, host="127.0.0.1", port=0,
                             heartbeat_timeout=0.3, ack_timeout=0.5)
    instance.start()
    client = FakeEA("127.0.0.1", instance.bound_port)
    try:
        assert instance.wait_connected(2.0)
        time.sleep(0.8)
        assert not instance.connected
        assert instance.stale
        client.send("heartbeat", {"equity": 100000.0})
        deadline = time.time() + 2.0
        while time.time() < deadline and not instance.connected:
            time.sleep(0.02)
        assert instance.connected
    finally:
        client.close()
        instance.stop()
