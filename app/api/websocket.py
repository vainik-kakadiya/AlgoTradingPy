"""Dashboard WebSocket. Read-only push channel: the backend stays authoritative and keeps
running regardless of whether any browser is connected."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.market_data.hub import hub
from app.services.event_bus import bus

router = APIRouter()


@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket) -> None:
    await ws.accept()
    queue = bus.subscribe()

    async def reader() -> None:
        while True:
            await ws.receive_text()  # client messages are ignored; raises on disconnect

    async def writer() -> None:
        while True:
            await ws.send_text(await queue.get())

    tasks: list[asyncio.Task[None]] = []
    try:
        await ws.send_text(json.dumps({"type": "hello", "data": {"execution_mode": "PAPER_ONLY"}}))
        bus.publish("feed", hub.status_dict())
        tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        bus.unsubscribe(queue)
