"""Loopback liveness check for the native EA bridge."""
from __future__ import annotations

import socket
import time


def bridge_check(host: str, port: int, timeout: float = 0.6) -> dict:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency = (time.perf_counter() - started) * 1000.0
        return {
            "listening": True,
            "host": host,
            "port": int(port),
            "latency_ms": round(latency, 1),
            "detail": "TCP port accepted a connection",
        }
    except OSError as exc:
        return {
            "listening": False,
            "host": host,
            "port": int(port),
            "detail": str(exc),
        }


__all__ = ["bridge_check"]
