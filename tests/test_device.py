from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anker_solix_mqtt.device import DeviceRuntime, DeviceSettings
from anker_solix_mqtt.models import DeviceMetadata
from anker_solix_mqtt.state_store import StateStore


class ScriptedDriver:
    def __init__(self, reads: list[dict[str, Any] | Exception]) -> None:
        self.reads = reads
        self.prepare_calls = 0
        self.closed = False

    async def prepare(self) -> DeviceMetadata:
        self.prepare_calls += 1
        return DeviceMetadata(device_id="solarbank", name="Solarbank Max AC")

    async def read(self) -> dict[str, Any]:
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_publishes_metadata_and_online_snapshot() -> None:
    store = StateStore()
    driver = ScriptedDriver([{"battery_soc": 82}])
    runtime = DeviceRuntime(DeviceSettings(device_id="solarbank", host="192.0.2.10"), store, driver)

    assert await runtime.poll_once()

    assert driver.prepare_calls == 1
    assert store.get_metadata("solarbank") is not None
    snapshot = store.get_snapshot("solarbank")
    assert snapshot is not None
    assert snapshot.online
    assert not snapshot.stale
    assert snapshot.error is None
    assert snapshot.values == {"battery_soc": 82}


@pytest.mark.asyncio
async def test_failed_poll_preserves_last_values_as_stale() -> None:
    store = StateStore()
    driver = ScriptedDriver([{"battery_soc": 82}, OSError("connection lost")])
    runtime = DeviceRuntime(DeviceSettings(device_id="solarbank", host="192.0.2.10"), store, driver)
    await runtime.poll_once()
    successful = store.get_snapshot("solarbank")

    assert not await runtime.poll_once()

    snapshot = store.get_snapshot("solarbank")
    assert snapshot is not None
    assert not snapshot.online
    assert snapshot.stale
    assert snapshot.error == "connection lost"
    assert snapshot.values == {"battery_soc": 82}
    assert snapshot.last_successful_poll == successful.last_successful_poll  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_run_closes_driver_when_already_stopped() -> None:
    driver = ScriptedDriver([])
    runtime = DeviceRuntime(
        DeviceSettings(device_id="solarbank", host="192.0.2.10"), StateStore(), driver
    )
    stop_event = asyncio.Event()
    stop_event.set()

    await runtime.run(stop_event)

    assert driver.closed
