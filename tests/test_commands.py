from typing import Any

import pytest

from anker_solix_mqtt.commands import CommandDispatcher
from anker_solix_mqtt.models import (
    ControlCommand,
    ControlOutcome,
    DeviceMetadata,
    EntityMetadata,
)
from anker_solix_mqtt.state_store import StateStore


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def execute(self, device_id: str, entity: EntityMetadata, value: Any) -> ControlOutcome:
        self.calls.append((device_id, entity.key, value))
        return ControlOutcome(success=True, value=value)


async def configured_dispatcher() -> tuple[CommandDispatcher, RecordingBackend]:
    store = StateStore()
    await store.publish_metadata(
        DeviceMetadata(
            device_id="solarbank",
            name="Solarbank",
            entities={
                "limit": EntityMetadata(
                    key="limit",
                    name="Leistungslimit",
                    kind="number",
                    writable=True,
                    min_value=100,
                    max_value=800,
                )
            },
        )
    )
    backend = RecordingBackend()
    return CommandDispatcher(store, backend), backend


@pytest.mark.asyncio
async def test_dispatches_valid_command_only_once() -> None:
    dispatcher, backend = await configured_dispatcher()
    command = ControlCommand(
        request_id="request-123",
        device_id="solarbank",
        entity_key="limit",
        value=600,
        source="web",
    )

    first = await dispatcher.dispatch(command)
    second = await dispatcher.dispatch(command)

    assert first.status == "success"
    assert second == first
    assert backend.calls == [("solarbank", "limit", 600.0)]


@pytest.mark.asyncio
async def test_rejects_out_of_range_value_before_backend() -> None:
    dispatcher, backend = await configured_dispatcher()

    result = await dispatcher.dispatch(
        ControlCommand(
            request_id="request-456",
            device_id="solarbank",
            entity_key="limit",
            value=900,
            source="mqtt",
        )
    )

    assert result.status == "rejected"
    assert result.error == "Wert darf höchstens 800 sein"
    assert backend.calls == []
