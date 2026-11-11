"""Versioned JSON protocol shared by the Python control plane and the MQL5 EA.

The wire format is documented in ``docs/PROTOCOL.md``.  This module deliberately
has no MT5 dependency so it can be unit-tested without a terminal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Mapping, Optional


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
TOKEN_ENV = "TRADING_AGENTS_EA_TOKEN"
DEFAULT_TOKEN_FILE = os.path.join("state", "ea_token.txt")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

KNOWN_STRATEGIES = ("trend", "breakout", "smc", "momentum", "mean_reversion")
KNOWN_STATUSES = ("filled", "duplicate", "rejected", "dry_run", "unknown")


class ProtocolError(ValueError):
    """A frame is malformed or violates the protocol contract."""


class AuthenticationError(ProtocolError):
    """The shared token is missing or wrong."""


class ClockSkewError(ProtocolError):
    """The peer clock is too far from local UTC to trade safely."""


class IntentError(ValueError):
    """An intent is structurally invalid before it reaches the EA."""


@dataclass(frozen=True)
class Envelope:
    type: str
    payload: dict
    id: str
    seq: int
    ts: float
    token: str = field(repr=False, compare=False)
    v: int = PROTOCOL_VERSION

    def clock_skew(self, now: Optional[float] = None) -> float:
        return abs((time.time() if now is None else float(now)) - float(self.ts))

    def to_json(self) -> str:
        body = {
            "v": int(self.v),
            "type": str(self.type),
            "id": str(self.id),
            "seq": int(self.seq),
            "ts": float(self.ts),
            "token": self.token,
            "payload": self.payload,
        }
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def to_bytes(self) -> bytes:
        return (self.to_json() + "\n").encode("utf-8")

    @classmethod
    def from_line(
        cls,
        line: str | bytes,
        token: str,
        *,
        now: Optional[float] = None,
        max_skew: float = 10.0,
    ) -> "Envelope":
        if isinstance(line, bytes):
            if len(line) > MAX_FRAME_BYTES:
                raise ProtocolError("frame exceeds the 1 MiB limit")
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("frame is not valid UTF-8") from exc
        else:
            text = line
            if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ProtocolError("frame exceeds the 1 MiB limit")
        text = text.strip()
        if not text:
            raise ProtocolError("empty frame")
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(body, dict):
            raise ProtocolError("frame must be a JSON object")

        version = body.get("v")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {version!r}")
        msg_type = body.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            raise ProtocolError("missing message type")
        msg_id = body.get("id")
        if not isinstance(msg_id, str) or not msg_id:
            raise ProtocolError("missing message id")
        seq = body.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ProtocolError("seq must be a positive integer")
        ts = body.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ProtocolError("ts must be epoch seconds")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be an object")
        supplied = body.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
            raise AuthenticationError("bad or missing token")

        envelope = cls(
            type=msg_type,
            payload=payload,
            id=msg_id,
            seq=seq,
            ts=float(ts),
            token=supplied,
            v=int(version),
        )
        if envelope.clock_skew(now) > float(max_skew):
            raise ClockSkewError(
                f"clock skew {envelope.clock_skew(now):.1f}s exceeds {max_skew:.1f}s")
        return envelope


@dataclass(frozen=True)
class Intent:
    idempotency_key: str
    symbol: str
    direction: int
    volume: float
    sl: float
    tp: float
    strategy: str
    entry: float = 0.0
    comment: str = ""
    risk_percent: Optional[float] = None
    max_slippage_points: Optional[int] = None
    expires_at: Optional[float] = None

    def validate(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) < 16:
            raise IntentError("idempotency_key is required")
        if not self.symbol:
            raise IntentError("symbol is required")
        if self.direction not in (-1, 1):
            raise IntentError("direction must be 1 or -1")
        if not (self.volume >= 0):
            raise IntentError("volume must be >= 0")
        if not (self.sl > 0 and self.tp > 0):
            raise IntentError("sl and tp must be positive prices")
        if self.strategy not in KNOWN_STRATEGIES:
            raise IntentError(f"unknown strategy: {self.strategy}")
        if self.risk_percent is not None and not (0 < self.risk_percent <= 100):
            raise IntentError("risk_percent must be in (0, 100]")
        if self.max_slippage_points is not None and self.max_slippage_points < 0:
            raise IntentError("max_slippage_points must be >= 0")
        if self.direction > 0 and not (self.sl < self.tp):
            raise IntentError("buy intent requires sl < tp")
        if self.direction < 0 and not (self.sl > self.tp):
            raise IntentError("sell intent requires sl > tp")

    def to_payload(self) -> dict:
        self.validate()
        payload = {
            "idempotency_key": self.idempotency_key,
            "symbol": self.symbol,
            "direction": int(self.direction),
            "volume": float(self.volume),
            "entry": float(self.entry),
            "sl": float(self.sl),
            "tp": float(self.tp),
            "strategy": self.strategy,
            "comment": self.comment or f"TA:{self.strategy}",
        }
        if self.risk_percent is not None:
            payload["risk_percent"] = float(self.risk_percent)
        if self.max_slippage_points is not None:
            payload["max_slippage_points"] = int(self.max_slippage_points)
        if self.expires_at is not None:
            payload["expires_at"] = float(self.expires_at)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Intent":
        try:
            intent = cls(
                idempotency_key=str(payload["idempotency_key"]),
                symbol=str(payload["symbol"]),
                direction=int(payload["direction"]),
                volume=float(payload.get("volume", 0.0)),
                sl=float(payload["sl"]),
                tp=float(payload["tp"]),
                strategy=str(payload["strategy"]),
                entry=float(payload.get("entry", 0.0) or 0.0),
                comment=str(payload.get("comment", "")),
                risk_percent=(float(payload["risk_percent"])
                              if payload.get("risk_percent") is not None else None),
                max_slippage_points=(int(payload["max_slippage_points"])
                                     if payload.get("max_slippage_points") is not None else None),
                expires_at=(float(payload["expires_at"])
                            if payload.get("expires_at") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntentError(f"invalid intent payload: {exc}") from exc
        intent.validate()
        return intent


@dataclass(frozen=True)
class Ack:
    idempotency_key: str
    status: str
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
    retcode: int = 0
    retcode_name: str = ""
    slippage_points: float = 0.0
    message: str = ""
    timestamp: float = 0.0

    @property
    def filled(self) -> bool:
        return self.status == "filled"

    @property
    def terminal_state(self) -> bool:
        return self.status in ("filled", "rejected", "duplicate", "dry_run")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Ack":
        status = str(payload.get("status", ""))
        if status not in KNOWN_STATUSES:
            raise ProtocolError(f"unknown ack status: {status!r}")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not key:
            raise ProtocolError("ack is missing idempotency_key")
        return cls(
            idempotency_key=key,
            status=status,
            ticket=_as_int(payload.get("ticket", 0)),
            price=_as_float(payload.get("price", 0.0)),
            volume=_as_float(payload.get("volume", 0.0)),
            retcode=_as_int(payload.get("retcode", 0)),
            retcode_name=str(payload.get("retcode_name", "")),
            slippage_points=_as_float(payload.get("slippage_points", 0.0)),
            message=str(payload.get("message", "")),
            timestamp=_as_float(payload.get("timestamp", 0.0)),
        )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def idempotency_key(strategy: str, symbol: str, bar_time: float | int,
                    direction: int) -> str:
    raw = f"{strategy}|{symbol}|{int(float(bar_time))}|{int(direction)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_envelope(msg_type: str, payload: Mapping[str, Any], token: str,
                  seq: int, *, msg_id: Optional[str] = None,
                  ts: Optional[float] = None) -> Envelope:
    return Envelope(
        type=msg_type,
        payload=dict(payload),
        id=msg_id or f"py-{seq}-{secrets.token_hex(3)}",
        seq=int(seq),
        ts=float(time.time() if ts is None else ts),
        token=token,
    )


def load_token(path: Optional[str] = None, *, env: str = TOKEN_ENV) -> str:
    value = os.environ.get(env, "").strip()
    if value:
        return value
    target = path or DEFAULT_TOKEN_FILE
    try:
        with open(target, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise AuthenticationError(
            f"EA token not found in {env} or {target}; run: python main.py ea-token"
        ) from exc
    if len(value) < 32:
        raise AuthenticationError(f"EA token in {target} is too short")
    return value


def create_token(path: Optional[str] = None, *, force: bool = False) -> tuple[str, str]:
    target = path or DEFAULT_TOKEN_FILE
    if os.path.exists(target) and not force:
        with open(target, encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value, target
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    value = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value, target


def redact(text: str, token: Optional[str]) -> str:
    if token:
        return text.replace(token, "<redacted>")
    return text


def token_matches(supplied: str, expected: str) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


__all__ = [
    "Ack",
    "AuthenticationError",
    "ClockSkewError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TOKEN_FILE",
    "Envelope",
    "Intent",
    "IntentError",
    "KNOWN_STATUSES",
    "KNOWN_STRATEGIES",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "TOKEN_ENV",
    "create_token",
    "idempotency_key",
    "load_token",
    "make_envelope",
    "redact",
    "token_matches",
]
