"""Authenticated WebSocket that pushes a compact snapshot on an interval."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from api.auth import token_matches
from api.services.overview import build_snapshot

log = logging.getLogger("api.ws")

router = APIRouter()


async def _send_snapshots(websocket: WebSocket, services) -> None:
    interval = max(0.05, float(services.settings.ws_interval))
    while True:
        snapshot = await run_in_threadpool(build_snapshot, services)
        await websocket.send_json(snapshot)
        await asyncio.sleep(interval)


async def _receive_control(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive_json()
        kind = str((message or {}).get("type") or "")
        if kind == "ping":
            await websocket.send_json({"type": "pong", "ts": message.get("ts")})
        elif kind == "close":
            await websocket.close(code=1000)
            return


@router.websocket("/ws")
async def live_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    services = websocket.app.state.services
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await websocket.close(code=4401)
        return

    token = str((first or {}).get("token") or "")
    if (first or {}).get("type") != "auth" or not token_matches(
            services.settings.dashboard_token, token):
        await websocket.close(code=4401)
        return

    await websocket.send_json({
        "type": "hello",
        "ts": services.clock(),
        "interval": max(0.05, float(services.settings.ws_interval)),
        "mode": "local",
    })

    tasks = [
        asyncio.create_task(_send_snapshots(websocket, services)),
        asyncio.create_task(_receive_control(websocket)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, RuntimeError):
                pass
    except WebSocketDisconnect:
        pass
    except Exception:                                    # noqa: BLE001
        log.exception("live socket failed")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
