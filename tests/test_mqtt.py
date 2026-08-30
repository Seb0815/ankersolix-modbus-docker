from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from anker_solix_mqtt.commands import CommandDispatcher
from anker_solix_mqtt.models import (
    ControlOutcome,
    DeviceMetadata,
    DeviceSnapshot,
    EntityMetadata,
)
from anker_solix_mqtt.mqtt import MQTTAdapter, MQTTPublication, MQTTSettings
from anker_solix_mqtt.state_store import StateStore


class FakeTransport:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.publications: list[MQTTPublication] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.on_connect: Callable[[], None] = lambda: None
        self.on_disconnect: Callable[[], None] = lambda: None
        self.on_message: Callable[[str, bytes], None] = lambda _topic, _payload: None

    def set_callbacks(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes], None],
    ) -> None:
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_message = on_message

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def publish(self, publication: MQTTPublication) -> None:
        self.publications.append(publication)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def execute(self, device_id: str, entity: EntityMetadata, value: Any) -> ControlOutcome:
        self.calls.append((device_id, entity.key, value))
        return ControlOutcome(success=True, value=value)


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _attempt in range(20):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


async def _adapter() -> tuple[MQTTAdapter, FakeTransport, StateStore, RecordingBackend]:
    store = StateStore()
    backend = RecordingBackend()
    dispatcher = CommandDispatcher(store, backend)
    transport = FakeTransport()
    adapter = MQTTAdapter(
        MQTTSettings(host="broker", topic_prefix="solix"),
        store,
        dispatcher,
        transport,
    )
    return adapter, transport, store, backend


@pytest.mark.asyncio
async def test_reconnect_publishes_current_retained_state() -> None:
    adapter, transport, store, _backend = await _adapter()
    await store.publish_metadata(DeviceMetadata(device_id="solarbank", name="Solarbank"))
    await store.publish_snapshot(
        DeviceSnapshot(device_id="solarbank", online=True, stale=False, values={"soc": 82})
    )

    await adapter.start()
    transport.on_connect()
    await _settle()

    assert transport.started
    assert transport.subscriptions == [("solix/+/command", 1)]
    by_topic = {item.topic: item for item in transport.publications}
    assert by_topic["solix/bridge/availability"].payload == "online"
    assert by_topic["solix/solarbank/availability"].payload == "online"
    assert json.loads(by_topic["solix/solarbank/state"].payload)["values"] == {"soc": 82}
    assert json.loads(by_topic["solix/solarbank/metadata"].payload)["name"] == "Solarbank"
    assert all(item.retain for item in by_topic.values())

    await adapter.stop()
    assert transport.stopped
    assert transport.publications[-1].payload == "offline"


@pytest.mark.asyncio
async def test_command_is_dispatched_and_result_is_published() -> None:
    adapter, transport, store, backend = await _adapter()
    await store.publish_metadata(
        DeviceMetadata(
            device_id="solarbank",
            name="Solarbank",
            entities={
                "limit": EntityMetadata(
                    key="limit",
                    name="Power limit",
                    kind="number",
                    writable=True,
                    min_value=0,
                    max_value=800,
                )
            },
        )
    )
    await adapter.start()
    transport.on_connect()
    await _settle()

    transport.on_message(
        "solix/solarbank/command",
        json.dumps({"request_id": "mqtt-1234", "entity_key": "limit", "value": 500}).encode(),
    )
    await _wait_for(
        lambda: any(
            item.topic == "solix/solarbank/command/result" for item in transport.publications
        )
    )

    assert backend.calls == [("solarbank", "limit", 500.0)]
    result = [
        item for item in transport.publications if item.topic == "solix/solarbank/command/result"
    ][-1]
    assert json.loads(result.payload)["status"] == "success"
    assert not result.retain
    await adapter.stop()


@pytest.mark.asyncio
async def test_offline_buffer_keeps_latest_retained_value() -> None:
    adapter, transport, store, _backend = await _adapter()
    await adapter.start()
    await store.publish_snapshot(DeviceSnapshot(device_id="solarbank", values={"soc": 10}))
    await store.publish_snapshot(DeviceSnapshot(device_id="solarbank", values={"soc": 20}))
    await _settle()

    assert transport.publications == []
    transport.on_connect()
    await _settle()

    states = [
        json.loads(item.payload)
        for item in transport.publications
        if item.topic == "solix/solarbank/state"
    ]
    assert states[-1]["values"] == {"soc": 20}
    await adapter.stop()


@pytest.mark.asyncio
async def test_invalid_command_returns_rejection_without_dispatch() -> None:
    adapter, transport, _store, backend = await _adapter()
    await adapter.start()
    transport.on_connect()
    await _settle()

    transport.on_message("solix/solarbank/command", b"not-json")
    await _settle()

    assert backend.calls == []
    result = transport.publications[-1]
    assert result.topic == "solix/solarbank/command/result"
    assert json.loads(result.payload)["status"] == "rejected"
    await adapter.stop()
