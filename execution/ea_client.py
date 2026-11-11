"""Loopback TCP bridge between Python and the native MQL5 EA.

The EA is the TCP client because MQL5 exposes outbound sockets only.  Python
listens, authenticates the first ``hello``, then exchanges newline-delimited
JSON envelopes.  See ``docs/PROTOCOL.md`` for the wire contract.

The bridge is intentionally independent of the runner: callers can use it for
paper trading, a supervised live mode, or tests.  It never retries an intent
whose ack timed out; that state becomes ``UNKNOWN`` and must be reconciled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import socket
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from execution.ea_protocol import (
    Ack,
    AuthenticationError,
    ClockSkewError,
    DEFAULT_HOST,
    DEFAULT_PORT,
    Envelope,
    Intent,
    MAX_FRAME_BYTES,
    ProtocolError,
    make_envelope,
    redact,
    token_matches,
)


log = logging.getLogger("ea_client")


class EAError(RuntimeError):
    """Base class for bridge errors."""


class EAUnavailable(EAError):
    """No authenticated, live EA is connected."""


class EATimeout(EAError):
    """An ack or reconciliation did not arrive in time.

    The outcome is unknown; call :meth:`EAOrderBridge.reconcile` before
    sending another intent for the same setup.
    """


class DuplicateIntentError(EAError):
    """The same idempotency key is pending or previously ended ambiguously."""


@dataclass
class _Pending:
    key: str
    msg_id: str
    event: threading.Event = field(default_factory=threading.Event)
    ack: Optional[Ack] = None
    error: str = ""
    created: float = field(default_factory=time.time)


@dataclass
class _Connection:
    sock: socket.socket
    address: tuple
    hello: dict = field(default_factory=dict)
    authenticated: bool = False
    peer_seq: int = 0
    last_heartbeat: float = 0.0
    opened_at: float = field(default_factory=time.time)


class EAOrderBridge:
    """Threaded TCP server that accepts one authenticated EA connection."""

    def __init__(
        self,
        token: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        heartbeat_timeout: float = 5.0,
        ack_timeout: float = 10.0,
        clock_skew_warn: float = 2.0,
        clock_skew_block: float = 10.0,
        server_heartbeat_interval: float = 2.0,
        on_positions: Optional[Callable[[list[dict]], None]] = None,
        on_stale: Optional[Callable[[bool], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        if not token or len(token) < 32:
            raise AuthenticationError("EA bridge token must be at least 32 characters")
        self.token = token
        self.host = host
        self.port = int(port)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.ack_timeout = float(ack_timeout)
        self.clock_skew_warn = float(clock_skew_warn)
        self.clock_skew_block = float(clock_skew_block)
        self.server_heartbeat_interval = float(server_heartbeat_interval)
        self.on_positions = on_positions
        self.on_stale = on_stale
        self.log = logger or log

        self._listen: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._conn: Optional[_Connection] = None
        self._seq = 0
        self._session_id = ""
        self._pending: dict[str, _Pending] = {}
        self._pending_by_msg: dict[str, _Pending] = {}
        self._known: dict[str, Ack] = {}
        self._unknown_keys: set[str] = set()
        self._positions: dict[int, dict] = {}
        self._reconcile_event = threading.Event()
        self._reconcile_result: dict = {}
        self._stale = False
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.settimeout(0.5)
            self._listen = listener
            self._stop.clear()
            self._started = True
            self._accept_thread = threading.Thread(
                target=self._accept_loop, name="ea-accept", daemon=True)
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="ea-monitor", daemon=True)
            self._accept_thread.start()
            self._monitor_thread.start()
            self.log.info("EA bridge listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conn = self._conn
            self._conn = None
            self._started = False
        if conn is not None:
            try:
                conn.sock.close()
            except OSError:
                pass
        listener = self._listen
        self._listen = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for thread in (self._accept_thread, self._monitor_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._fail_all_pending("bridge stopped")
        self.log.info("EA bridge stopped")

    def __enter__(self) -> "EAOrderBridge":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------ properties
    @property
    def connected(self) -> bool:
        conn = self._conn
        if conn is None or not conn.authenticated:
            return False
        if self._stale:
            return False
        return (time.time() - conn.last_heartbeat) <= self.heartbeat_timeout

    @property
    def stale(self) -> bool:
        return self._stale

    @property
    def bound_port(self) -> int:
        listener = self._listen
        if listener is None:
            return self.port
        try:
            return int(listener.getsockname()[1])
        except OSError:
            return self.port

    @property
    def hello(self) -> dict:
        conn = self._conn
        return dict(conn.hello) if conn is not None else {}

    def positions(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._positions.values()]

    def unknown_keys(self) -> set[str]:
        with self._lock:
            return set(self._unknown_keys)

    def status(self) -> dict:
        conn = self._conn
        return {
            "listening": self._started,
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "stale": self._stale,
            "authenticated": bool(conn and conn.authenticated),
            "peer": conn.address if conn else None,
            "hello": dict(conn.hello) if conn else {},
            "last_heartbeat_age": (time.time() - conn.last_heartbeat) if conn else None,
            "positions": len(self._positions),
            "pending": len(self._pending),
            "unknown_keys": sorted(self._unknown_keys),
        }

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self.connected:
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------ intents
    def send_intent(self, intent: Intent, *, timeout: Optional[float] = None,
                    wait: bool = True) -> Optional[Ack]:
        intent.validate()
        if not self.connected:
            raise EAUnavailable("EA is not connected or heartbeat is stale")

        key = intent.idempotency_key
        with self._lock:
            cached = self._known.get(key)
            if cached is not None:
                if cached.status in ("filled", "duplicate", "dry_run"):
                    return cached
                raise DuplicateIntentError(
                    f"intent {key[:12]} previously ended as {cached.status}; reconcile first")
            if key in self._pending:
                raise DuplicateIntentError(
                    f"intent {key[:12]} is already pending; do not retry")
            if key in self._unknown_keys:
                raise DuplicateIntentError(
                    f"intent {key[:12]} has an unknown outcome; reconcile first")

        pending = _Pending(key=key, msg_id="")
        with self._lock:
            self._pending[key] = pending

        try:
            envelope = self._send("intent", intent.to_payload())
        except EAError:
            with self._lock:
                self._pending.pop(key, None)
                self._pending_by_msg.pop(pending.msg_id, None)
            raise
        pending.msg_id = envelope.id
        with self._lock:
            self._pending_by_msg[envelope.id] = pending

        if not wait:
            return None
        return self._wait_for_ack(pending, self.ack_timeout if timeout is None else timeout)

    def close_position(self, ticket: int, reason: str = "python close",
                       *, timeout: Optional[float] = None) -> Ack:
        key = f"close-{int(ticket)}-{int(time.time() * 1000)}"
        pending = _Pending(key=key, msg_id="")
        with self._lock:
            self._pending[key] = pending
        try:
            envelope = self._send("close_request", {
                "ticket": int(ticket),
                "idempotency_key": key,
                "reason": str(reason)[:120],
            })
        except EAError:
            with self._lock:
                self._pending.pop(key, None)
            raise
        pending.msg_id = envelope.id
        with self._lock:
            self._pending_by_msg[envelope.id] = pending
        return self._wait_for_ack(pending, self.ack_timeout if timeout is None else timeout)

    def halt(self, reason: str, *, flatten: bool = False,
             until: Optional[float] = None) -> None:
        self._send("halt", {
            "reason": str(reason)[:200],
            "flatten": bool(flatten),
            "until": float(until) if until is not None else 0.0,
        })

    def resume(self) -> None:
        self._send("resume", {"reason": "python resume"})

    def reconcile(self, *, positions: Optional[Iterable[Mapping[str, Any]]] = None,
                  since: float = 0.0, timeout: float = 10.0) -> dict:
        if not self.connected:
            raise EAUnavailable("EA is not connected")
        self._reconcile_event.clear()
        self._reconcile_result = {}
        payload = {
            "since": float(since),
            "positions": [{"ticket": int(p.get("ticket", 0)),
                           "volume": float(p.get("volume", 0.0))}
                          for p in (positions or self.positions())],
            "pending_keys": sorted(self._unknown_keys),
        }
        self._send("reconcile_request", payload)
        if not self._reconcile_event.wait(timeout):
            raise EATimeout("reconciliation timed out; broker state is unknown")
        with self._lock:
            return dict(self._reconcile_result)

    # ------------------------------------------------------------------ send path
    def _send(self, msg_type: str, payload: Mapping[str, Any]) -> Envelope:
        with self._send_lock:
            with self._lock:
                conn = self._conn
                if conn is None or not conn.authenticated:
                    raise EAUnavailable("EA is not connected")
                self._seq += 1
                seq = self._seq
            envelope = make_envelope(msg_type, payload, self.token, seq)
            try:
                conn.sock.sendall(envelope.to_bytes())
            except OSError as exc:
                self.log.warning("EA send failed: %s", exc)
                self._drop_connection(conn, "send failed")
                raise EAUnavailable(f"EA send failed: {exc}") from exc
            self.log.debug("-> EA %s seq=%s", msg_type, seq)
            return envelope

    # ------------------------------------------------------------------ accept/read
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listen
            if listener is None:
                break
            try:
                sock, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.settimeout(1.0)
            thread = threading.Thread(
                target=self._serve_connection, args=(sock, address),
                name="ea-conn", daemon=True)
            thread.start()

    def _serve_connection(self, sock: socket.socket, address: tuple) -> None:
        conn = _Connection(sock=sock, address=address)
        try:
            first = self._read_line(sock, deadline=time.time() + 5.0)
            if first is None:
                self.log.warning("EA connection from %s sent no hello", address)
                return
            try:
                envelope = Envelope.from_line(
                    first, self.token, now=time.time(), max_skew=self.clock_skew_block)
            except AuthenticationError as exc:
                self.log.warning("EA auth failed from %s: %s", address, exc)
                return
            except (ProtocolError, ClockSkewError) as exc:
                self.log.warning("EA hello rejected from %s: %s", address, exc)
                return
            if envelope.type != "hello" or envelope.payload.get("role") != "ea":
                self.log.warning("EA from %s did not start with a valid hello", address)
                return

            conn.hello = dict(envelope.payload)
            conn.authenticated = True
            conn.peer_seq = envelope.seq
            conn.last_heartbeat = time.time()

            with self._lock:
                old = self._conn
                self._conn = conn
                self._seq = 0
                self._session_id = f"ea-{int(time.time())}"
                self._stale = False
            if old is not None and old.sock is not sock:
                try:
                    old.sock.close()
                except OSError:
                    pass
            self.log.info(
                "EA connected from %s account=%s symbol=%s mode=%s",
                address, conn.hello.get("account"), conn.hello.get("symbol"),
                conn.hello.get("mode"))
            self._send("hello", {
                "role": "server",
                "version": "1.0.0",
                "session_id": self._session_id,
                "server_time": time.time(),
                "heartbeat_ms": int(self.server_heartbeat_interval * 1000),
            })
            self._notify_stale(False)

            self._read_loop(conn)
        except Exception as exc:  # pragma: no cover - defensive network path
            self.log.exception("EA connection error: %s", exc)
        finally:
            self._drop_connection(conn, "connection ended")
            try:
                sock.close()
            except OSError:
                pass

    def _read_loop(self, conn: _Connection) -> None:
        buffer = b""
        while not self._stop.is_set() and self._conn is conn:
            try:
                chunk = conn.sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_FRAME_BYTES and b"\n" not in buffer:
                self.log.warning("EA frame exceeds limit; closing connection")
                break
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if len(line) > MAX_FRAME_BYTES:
                    self.log.warning("EA frame exceeds limit; dropping line")
                    continue
                self._handle_line(conn, line)

    def _handle_line(self, conn: _Connection, line: bytes) -> None:
        try:
            envelope = Envelope.from_line(
                line, self.token, now=time.time(), max_skew=self.clock_skew_block)
        except ClockSkewError as exc:
            self.log.warning("EA clock skew: %s", exc)
            self._safe_send("reject", {"reason": str(exc), "id": ""})
            return
        except (ProtocolError, AuthenticationError) as exc:
            self.log.warning("EA frame rejected: %s", exc)
            return

        if envelope.seq <= conn.peer_seq:
            self.log.debug("EA duplicate/old seq=%s (last=%s)", envelope.seq, conn.peer_seq)
            return
        if envelope.seq > conn.peer_seq + 1 and conn.peer_seq:
            self.log.warning("EA sequence gap: expected %s got %s",
                             conn.peer_seq + 1, envelope.seq)
            self._safe_send("resync", {
                "expected_seq": conn.peer_seq + 1,
                "received_seq": envelope.seq,
            })
        conn.peer_seq = envelope.seq
        conn.last_heartbeat = time.time()
        if self._stale:
            self._stale = False
            self._notify_stale(False)

        msg_type = envelope.type
        payload = envelope.payload
        if msg_type == "hello":
            conn.hello.update(payload)
        elif msg_type == "heartbeat":
            self.log.debug("EA heartbeat %s", payload)
        elif msg_type == "ack":
            self._handle_ack(payload)
        elif msg_type == "reject":
            self._handle_reject(payload)
        elif msg_type == "position_update":
            self._handle_positions(payload)
        elif msg_type == "reconcile_result":
            with self._lock:
                self._reconcile_result = dict(payload)
            self._reconcile_event.set()
            self._handle_positions(payload)
            self.log.info("EA reconciliation: %s mismatches",
                          len(payload.get("mismatches") or []))
        elif msg_type in ("error", "resync"):
            self.log.warning("EA %s: %s", msg_type, payload)
        else:
            self.log.warning("EA sent unknown message type %s", msg_type)

    def _handle_ack(self, payload: Mapping[str, Any]) -> None:
        try:
            ack = Ack.from_payload(payload)
        except ProtocolError as exc:
            self.log.warning("EA invalid ack: %s", exc)
            return
        with self._lock:
            pending = self._pending.pop(ack.idempotency_key, None)
            if pending is not None:
                self._pending_by_msg.pop(pending.msg_id, None)
            if ack.status in ("filled", "duplicate", "dry_run"):
                self._known[ack.idempotency_key] = ack
            elif ack.status == "rejected":
                self._known[ack.idempotency_key] = ack
            elif ack.status == "unknown":
                self._unknown_keys.add(ack.idempotency_key)
            if ack.ticket:
                if ack.status in ("filled", "dry_run"):
                    self._positions.setdefault(ack.ticket, {
                        "ticket": ack.ticket,
                        "price": ack.price,
                        "volume": ack.volume,
                        "status": ack.status,
                    })
            if ack.status == "duplicate":
                self._unknown_keys.discard(ack.idempotency_key)
        if pending is None:
            self.log.info("EA ack without a waiter: %s %s",
                          ack.idempotency_key[:12], ack.status)
            return
        pending.ack = ack
        pending.event.set()
        self.log.info("EA ack %s ticket=%s price=%s",
                      ack.status, ack.ticket, ack.price)

    def _handle_reject(self, payload: Mapping[str, Any]) -> None:
        key = str(payload.get("idempotency_key", ""))
        msg_id = str(payload.get("id", ""))
        with self._lock:
            pending = self._pending.pop(key, None) if key else None
            if pending is None and msg_id:
                pending = self._pending_by_msg.pop(msg_id, None)
                if pending is not None:
                    self._pending.pop(pending.key, None)
        if pending is not None:
            pending.error = str(payload.get("reason", "rejected by EA"))
            pending.event.set()

    def _handle_positions(self, payload: Mapping[str, Any]) -> None:
        positions = payload.get("positions")
        if not isinstance(positions, list):
            return
        with self._lock:
            for item in positions:
                if not isinstance(item, Mapping):
                    continue
                try:
                    ticket = int(item.get("ticket", 0))
                except (TypeError, ValueError):
                    continue
                if not ticket:
                    continue
                self._positions[ticket] = dict(item)
            self._positions = {
                ticket: item for ticket, item in self._positions.items()
                if item.get("status") not in ("closed", "missing")
            }
        if self.on_positions:
            try:
                self.on_positions(self.positions())
            except Exception:  # pragma: no cover - callback isolation
                self.log.exception("position callback failed")

    # ------------------------------------------------------------------ waits
    def _wait_for_ack(self, pending: _Pending, timeout: float) -> Ack:
        if not pending.event.wait(max(0.0, float(timeout))):
            with self._lock:
                self._pending.pop(pending.key, None)
                self._pending_by_msg.pop(pending.msg_id, None)
                self._unknown_keys.add(pending.key)
            raise EATimeout(
                f"ack for {pending.key[:12]} timed out; outcome UNKNOWN, reconcile first")
        if pending.error:
            with self._lock:
                self._unknown_keys.add(pending.key)
            raise EAError(pending.error)
        if pending.ack is None:
            raise EATimeout("ack waiter completed without a result")
        return pending.ack

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            self._pending_by_msg.clear()
        for item in pending:
            item.error = reason
            item.event.set()

    # ------------------------------------------------------------------ monitor
    def _monitor_loop(self) -> None:
        last_send = 0.0
        while not self._stop.is_set():
            now = time.time()
            conn = self._conn
            if conn is not None and conn.authenticated:
                if now - conn.last_heartbeat > self.heartbeat_timeout:
                    if not self._stale:
                        self._stale = True
                        self.log.warning("EA heartbeat stale for %.1fs",
                                         now - conn.last_heartbeat)
                        self._notify_stale(True)
                if now - last_send >= self.server_heartbeat_interval:
                    try:
                        self._send("heartbeat", {
                            "server_time": now,
                            "desired_mode": "live",
                            "halted": False,
                        })
                    except EAError:
                        pass
                    last_send = now
            else:
                last_send = 0.0
            time.sleep(0.25)

    # ------------------------------------------------------------------ helpers
    def _safe_send(self, msg_type: str, payload: Mapping[str, Any]) -> None:
        try:
            self._send(msg_type, payload)
        except EAError:
            pass

    def _drop_connection(self, conn: _Connection, reason: str) -> None:
        with self._lock:
            current = self._conn is conn
            if current:
                self._conn = None
                self._stale = False
        if current:
            self.log.info("EA disconnected: %s", reason)
            self._notify_stale(False)

    def _notify_stale(self, value: bool) -> None:
        if self.on_stale:
            try:
                self.on_stale(bool(value))
            except Exception:  # pragma: no cover - callback isolation
                self.log.exception("stale callback failed")

    @staticmethod
    def _read_line(sock: socket.socket, *, deadline: float) -> Optional[bytes]:
        buffer = b""
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buffer += chunk
            if b"\n" in buffer:
                return buffer.split(b"\n", 1)[0].strip()
            if len(buffer) > MAX_FRAME_BYTES:
                return None
        return None


__all__ = [
    "DuplicateIntentError",
    "EAError",
    "EAOrderBridge",
    "EATimeout",
    "EAUnavailable",
]
