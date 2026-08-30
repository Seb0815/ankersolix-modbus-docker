import pytest

from anker_solix_mqtt.models import DeviceMetadata, DeviceSnapshot
from anker_solix_mqtt.state_store import StateStore


@pytest.mark.asyncio
async def test_publishes_revisioned_snapshot() -> None:
    store = StateStore()

    stored = await store.publish_snapshot(
        DeviceSnapshot(device_id="solarbank", online=True, stale=False, values={"soc": 82})
    )

    assert stored.revision == 1
    assert store.get_snapshot("solarbank") == stored
    assert store.list_snapshots() == (stored,)


@pytest.mark.asyncio
async def test_slow_subscriber_receives_latest_event() -> None:
    store = StateStore(subscriber_queue_size=1)

    async with store.subscribe() as queue:
        await store.publish_snapshot(DeviceSnapshot(device_id="solarbank", values={"soc": 81}))
        await store.publish_metadata(DeviceMetadata(device_id="solarbank", name="Solarbank"))

        event = await queue.get()

    assert event.event == "metadata"
    assert event.revision == 2


def test_rejects_invalid_queue_size() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        StateStore(subscriber_queue_size=0)
