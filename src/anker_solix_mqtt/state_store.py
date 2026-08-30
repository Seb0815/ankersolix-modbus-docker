"""In-memory source of truth shared by all adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from .models import DeviceMetadata, DeviceSnapshot, StateEvent


class StateStore:
    """Store immutable snapshots and fan out non-blocking update events."""

    def __init__(self, subscriber_queue_size: int = 8) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        self._queue_size = subscriber_queue_size
        self._snapshots: dict[str, DeviceSnapshot] = {}
        self._metadata: dict[str, DeviceMetadata] = {}
        self._subscribers: set[asyncio.Queue[StateEvent]] = set()
        self._revision = 0
        self._lock = asyncio.Lock()

    @property
    def revision(self) -> int:
        """Return the latest global state revision."""
        return self._revision

    def list_snapshots(self) -> tuple[DeviceSnapshot, ...]:
        """Return a stable view sorted by device id."""
        return tuple(self._snapshots[key] for key in sorted(self._snapshots))

    def get_snapshot(self, device_id: str) -> DeviceSnapshot | None:
        return self._snapshots.get(device_id)

    def get_metadata(self, device_id: str) -> DeviceMetadata | None:
        return self._metadata.get(device_id)

    def list_metadata(self) -> tuple[DeviceMetadata, ...]:
        """Return device metadata sorted by device id."""
        return tuple(self._metadata[key] for key in sorted(self._metadata))

    async def publish_snapshot(self, snapshot: DeviceSnapshot) -> DeviceSnapshot:
        """Store a snapshot with a new revision and notify subscribers."""
        async with self._lock:
            self._revision += 1
            stored = snapshot.model_copy(update={"revision": self._revision})
            self._snapshots[stored.device_id] = stored
            event = StateEvent(
                event="state",
                device_id=stored.device_id,
                revision=self._revision,
                data=stored.model_dump(mode="json"),
            )
            self._fan_out(event)
            return stored

    async def publish_metadata(self, metadata: DeviceMetadata) -> DeviceMetadata:
        """Store metadata and notify subscribers."""
        async with self._lock:
            self._revision += 1
            self._metadata[metadata.device_id] = metadata
            event = StateEvent(
                event="metadata",
                device_id=metadata.device_id,
                revision=self._revision,
                data=metadata.model_dump(mode="json"),
            )
            self._fan_out(event)
            return metadata

    async def publish_event(
        self, event_type: str, device_id: str | None, data: dict[str, object]
    ) -> StateEvent:
        """Publish an adapter-neutral runtime event."""
        if event_type not in {"device_status", "command_result"}:
            raise ValueError(f"unsupported event type: {event_type}")
        async with self._lock:
            self._revision += 1
            event = StateEvent.model_validate(
                {
                    "event": event_type,
                    "device_id": device_id,
                    "revision": self._revision,
                    "data": data,
                }
            )
            self._fan_out(event)
            return event

    @asynccontextmanager
    async def subscribe(self) -> AsyncGenerator[asyncio.Queue[StateEvent], None]:
        """Subscribe with a bounded queue; oldest events yield to current state."""
        queue: asyncio.Queue[StateEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def _fan_out(self, event: StateEvent) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
