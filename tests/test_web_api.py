from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from anker_solix_mqtt.commands import CommandDispatcher
from anker_solix_mqtt.models import (
    ControlOutcome,
    DeviceMetadata,
    DeviceSnapshot,
    EntityMetadata,
)
from anker_solix_mqtt.state_store import StateStore
from anker_solix_mqtt.web.api import format_sync_event
from anker_solix_mqtt.web.app import create_web_app
from anker_solix_mqtt.web.security import CSRF_COOKIE, CSRF_HEADER


class SuccessfulBackend:
    async def execute(self, device_id: str, entity: EntityMetadata, value: Any) -> ControlOutcome:
        return ControlOutcome(success=True, value=value)


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    store = StateStore()
    await store.publish_metadata(
        DeviceMetadata(
            device_id="solarbank",
            name="Solarbank Max AC",
            entities={
                "enabled": EntityMetadata(key="enabled", name="Aktiv", kind="switch", writable=True)
            },
        )
    )
    await store.publish_snapshot(
        DeviceSnapshot(device_id="solarbank", online=True, stale=False, values={"soc": 82})
    )
    app = create_web_app(store, CommandDispatcher(store, SuccessfulBackend()))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as test_client:
        await test_client.get("/api/v1/system")
        yield test_client


@pytest.mark.asyncio
async def test_returns_shared_device_state(client: AsyncClient) -> None:
    response = await client.get("/api/v1/devices/solarbank/state")

    assert response.status_code == 200
    assert response.json()["values"] == {"soc": 82}
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_renders_dashboard_from_shared_metadata(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "SOLIX Control" in response.text
    assert "Solarbank Max AC" in response.text
    assert 'data-entity="enabled"' in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


def test_sse_sync_contains_current_revision() -> None:
    assert format_sync_event(2) == 'event: sync\ndata: {"revision": 2}\n\n'


@pytest.mark.asyncio
async def test_command_requires_matching_origin_and_csrf(client: AsyncClient) -> None:
    token = client.cookies[CSRF_COOKIE]
    response = await client.post(
        "/api/v1/devices/solarbank/commands",
        json={"request_id": "web-request-1", "entity_key": "enabled", "value": True},
        headers={CSRF_HEADER: token, "Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.asyncio
async def test_rejects_cross_origin_command(client: AsyncClient) -> None:
    token = client.cookies[CSRF_COOKIE]
    response = await client.post(
        "/api/v1/devices/solarbank/commands",
        json={"request_id": "web-request-2", "entity_key": "enabled", "value": True},
        headers={CSRF_HEADER: token, "Origin": "http://attacker.invalid"},
    )

    assert response.status_code == 403
