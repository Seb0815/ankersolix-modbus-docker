from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from anker_solix_mqtt.app import AppSettings, BridgeRuntime, WebSettings, load_settings
from anker_solix_mqtt.device import DeviceSettings
from anker_solix_mqtt.models import DeviceMetadata
from anker_solix_mqtt.mqtt import MQTTPublication, MQTTSettings
from anker_solix_mqtt.official import DriverWriteResult


class FakeTransport:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.on_connect: Callable[[], None] = lambda: None

    def set_callbacks(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes], None],
    ) -> None:
        self.on_connect = on_connect
        _ = on_disconnect, on_message

    def start(self) -> None:
        self.started = True
        self.on_connect()

    def stop(self) -> None:
        self.stopped = True

    def subscribe(self, topic: str, qos: int) -> None:
        _ = topic, qos

    def publish(self, publication: MQTTPublication) -> None:
        _ = publication


class FakeDriver:
    def __init__(self, settings: DeviceSettings) -> None:
        self.settings = settings
        self.closed = False
        self.reads = 0

    @property
    def profile(self) -> None:
        return None

    async def prepare(self) -> DeviceMetadata:
        return DeviceMetadata(device_id=self.settings.device_id, name="Test device")

    async def read(self) -> dict[str, Any]:
        self.reads += 1
        return {"soc": 42}

    async def write_raw(self, address: int, value: Any, data_type: str) -> DriverWriteResult:
        _ = address, value, data_type
        raise AssertionError("write not expected")

    async def close(self) -> None:
        self.closed = True


def _settings() -> AppSettings:
    return AppSettings(
        devices=(DeviceSettings(device_id="solarbank", host="192.0.2.10"),),
        mqtt=MQTTSettings(host="broker"),
        web=WebSettings(port=8080),
    )


def test_rejects_duplicate_device_ids() -> None:
    device = DeviceSettings(device_id="solarbank", host="192.0.2.10")
    with pytest.raises(ValidationError, match="device_id must be unique"):
        AppSettings(devices=(device, device), mqtt=MQTTSettings(host="broker"))


def test_loads_nested_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ANKER_DEVICES",
        '[{"device_id":"solarbank","host":"192.0.2.10","poll_interval":7}]',
    )
    monkeypatch.setenv("ANKER_MQTT__HOST", "192.0.2.20")
    monkeypatch.setenv("ANKER_MQTT__USERNAME", "")
    monkeypatch.setenv("ANKER_WEB__PORT", "8090")

    settings = load_settings()

    assert settings.devices[0].poll_interval == 7
    assert settings.mqtt.host == "192.0.2.20"
    assert settings.mqtt.username is None
    assert settings.web.port == 8090


@pytest.mark.asyncio
async def test_runtime_starts_polling_and_closes_resources() -> None:
    drivers: list[FakeDriver] = []

    def create_driver(settings: DeviceSettings) -> FakeDriver:
        driver = FakeDriver(settings)
        drivers.append(driver)
        return driver

    transport = FakeTransport()
    runtime = BridgeRuntime(
        _settings(),
        driver_factory=create_driver,
        mqtt_transport=transport,
    )

    await runtime.start()
    for _attempt in range(20):
        if runtime.store.get_snapshot("solarbank") is not None:
            break
        await asyncio.sleep(0)
    await runtime.stop()

    snapshot = runtime.store.get_snapshot("solarbank")
    assert snapshot is not None
    assert snapshot.values == {"soc": 42}
    assert transport.started and transport.stopped
    assert drivers[0].reads == 1
    assert drivers[0].closed
