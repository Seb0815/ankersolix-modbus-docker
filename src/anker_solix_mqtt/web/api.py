"""HTTP API backed only by shared application services."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..commands import CommandDispatcher
from ..models import ControlCommand
from ..state_store import StateStore


class WebCommand(BaseModel):
    """Public command body; device and source come from trusted routing."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    entity_key: str = Field(min_length=1, max_length=128)
    value: Any


def format_sync_event(revision: int) -> str:
    """Format the initial SSE synchronization event."""
    return f"event: sync\ndata: {json.dumps({'revision': revision})}\n\n"


def format_state_event(event_id: int, event_type: str, payload: str) -> str:
    """Format one revisioned SSE event."""
    return f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"


def create_api_router(store: StateStore, dispatcher: CommandDispatcher) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    async def _get_system() -> dict[str, object]:
        return {"status": "running", "revision": store.revision}

    async def _get_devices() -> dict[str, object]:
        devices: list[dict[str, object]] = []
        for metadata in store.list_metadata():
            snapshot = store.get_snapshot(metadata.device_id)
            devices.append(
                {
                    "device_id": metadata.device_id,
                    "name": metadata.name,
                    "model": metadata.model,
                    "online": snapshot.online if snapshot else False,
                    "stale": snapshot.stale if snapshot else True,
                    "revision": snapshot.revision if snapshot else 0,
                }
            )
        return {"devices": devices}

    async def _get_state(device_id: str) -> dict[str, object]:
        snapshot = store.get_snapshot(device_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        return snapshot.model_dump(mode="json")

    async def _get_metadata(device_id: str) -> dict[str, object]:
        metadata = store.get_metadata(device_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        return metadata.model_dump(mode="json")

    async def _get_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncGenerator[str, None]:
            yield format_sync_event(store.revision)
            async with store.subscribe() as queue:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield format_state_event(event.revision, event.event, event.model_dump_json())

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def _post_command(
        device_id: str, body: WebCommand, request: Request
    ) -> dict[str, object]:
        del request
        result = await dispatcher.dispatch(
            ControlCommand(
                request_id=body.request_id,
                device_id=device_id,
                entity_key=body.entity_key,
                value=body.value,
                source="web",
            )
        )
        return result.model_dump(mode="json")

    router.add_api_route("/system", _get_system, methods=["GET"])
    router.add_api_route("/devices", _get_devices, methods=["GET"])
    router.add_api_route("/devices/{device_id}/state", _get_state, methods=["GET"])
    router.add_api_route("/devices/{device_id}/metadata", _get_metadata, methods=["GET"])
    router.add_api_route("/events", _get_events, methods=["GET"])
    router.add_api_route("/devices/{device_id}/commands", _post_command, methods=["POST"])
    return router
