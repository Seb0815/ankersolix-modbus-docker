"""Per-device polling lifecycle independent of transport adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .models import DeviceMetadata, DeviceSnapshot, utc_now
from .state_store import StateStore

_LOGGER = logging.getLogger(__name__)


class DeviceSettings(BaseModel):
    """Connection and scheduling settings for one Modbus device."""

    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=502, ge=1, le=65535)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    poll_interval: float = Field(default=5.0, gt=0)
    retry_interval: float = Field(default=10.0, gt=0)


class DeviceDriver(Protocol):
    """One device connection hidden behind the runtime boundary."""

    async def prepare(self) -> DeviceMetadata: ...

    async def read(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class DeviceRuntime:
    """Poll one device and publish immutable snapshots to the shared store."""

    def __init__(self, settings: DeviceSettings, store: StateStore, driver: DeviceDriver) -> None:
        self.settings = settings
        self._store = store
        self._driver = driver
        self._prepared = False

    async def poll_once(self) -> bool:
        """Run one prepare/read cycle and publish online or stale state."""
        try:
            if not self._prepared:
                metadata = await self._driver.prepare()
                if metadata.device_id != self.settings.device_id:
                    raise ValueError("driver metadata device_id does not match runtime")
                await self._store.publish_metadata(metadata)
                self._prepared = True

            values = await self._driver.read()
            if not values:
                raise ConnectionError("Modbus query returned no values")
        except Exception as exc:
            _LOGGER.warning("Device %s poll failed: %s", self.settings.device_id, exc)
            await self._publish_failure(str(exc))
            return False

        now = utc_now()
        await self._store.publish_snapshot(
            DeviceSnapshot(
                device_id=self.settings.device_id,
                timestamp=now,
                last_successful_poll=now,
                online=True,
                stale=False,
                values=values,
            )
        )
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll until stopped, using the shorter device cadence after success."""
        try:
            while not stop_event.is_set():
                succeeded = await self.poll_once()
                delay = self.settings.poll_interval if succeeded else self.settings.retry_interval
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            await self._driver.close()

    async def _publish_failure(self, error: str) -> None:
        previous = self._store.get_snapshot(self.settings.device_id)
        await self._store.publish_snapshot(
            DeviceSnapshot(
                device_id=self.settings.device_id,
                last_successful_poll=previous.last_successful_poll if previous else None,
                online=False,
                stale=True,
                error=error,
                values=previous.values if previous else {},
            )
        )
