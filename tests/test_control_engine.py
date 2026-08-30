from __future__ import annotations

from typing import Any

import pytest

from anker_solix_mqtt.control_engine import ControlEngine
from anker_solix_mqtt.models import DeviceSnapshot
from anker_solix_mqtt.official import DriverWriteResult
from anker_solix_mqtt.profile import DeviceProfile, build_device_profile
from anker_solix_mqtt.state_store import StateStore


class RecordingDevice:
    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile
        self.writes: list[tuple[int, Any, str]] = []

    async def write_raw(self, address: int, value: Any, data_type: str) -> DriverWriteResult:
        self.writes.append((address, value, data_type))
        return DriverWriteResult(success=True)


def _profile() -> DeviceProfile:
    controls: dict[str, dict[str, Any]] = {
        "battery_power_setpoint": {
            "address": 10071,
            "data_type": "INT32",
            "gain": 1,
            "min_value": 0,
            "max_value": 10000,
            "direction_entity": "battery_power_direction",
            "max_charge_power_entity": "max_charge_power",
            "max_discharge_power_entity": "max_discharge_power",
            "visibility_entity": "operating_mode",
            "visibility_value": 3,
            "value_constraints": {"rules": [{"type": "warning_range", "min": 1, "max": 99}]},
        },
        "charging_limit_soc": {
            "address": 60000,
            "data_type": "UINT16",
            "min_value": 80,
            "max_value": 100,
            "capability_entity": "parallel_capability_mask",
            "capability_bit": 0,
            "soc_validation": {
                "condition_entity": "backup_soc_enable",
                "condition_value": 1,
                "greater_than": ["backup_reserve_soc"],
            },
        },
        "operating_mode": {
            "address": 10064,
            "data_type": "UINT16",
            "control_type": "select",
            "capability_entity": "ems_mode_mask",
            "options": {"0": "self_consumption", "3": "third_party_control"},
            "option_capability_bits": {"0": 0, "3": 5},
        },
        "battery_power_direction": {
            "address": 10071,
            "data_type": "INT32",
            "control_type": "select",
            "options": {"0": "charge", "1": "discharge"},
            "is_direction_selector": True,
            "visibility_entity": "operating_mode",
            "visibility_value": 3,
        },
    }
    config: dict[str, Any] = {
        "product_info": {"default_name": "Solarbank Max AC"},
        "read_quantities": {
            "max_charge_power": {"internal": True},
            "max_discharge_power": {"internal": True},
            "parallel_capability_mask": {"internal": True},
            "ems_mode_mask": {"internal": True},
            "backup_soc_enable": {"internal": True},
            "backup_reserve_soc": {},
        },
        "control_items": {
            "battery_power_setpoint": controls["battery_power_setpoint"],
            "charging_limit_soc": controls["charging_limit_soc"],
        },
        "write_quantities": {
            "enumeration_selection": {
                "operating_mode": controls["operating_mode"],
                "battery_power_direction": controls["battery_power_direction"],
            }
        },
    }
    data_points: dict[str, Any] = {
        **config["read_quantities"],
        **config["control_items"],
        **config["write_quantities"]["enumeration_selection"],
    }
    return build_device_profile("solarbank", None, config, data_points, [], {})


async def _engine(state: dict[str, Any]):
    store = StateStore()
    profile = _profile()
    await store.publish_metadata(profile.metadata)
    await store.publish_snapshot(
        DeviceSnapshot(device_id="solarbank", online=True, stale=False, values=state)
    )
    device = RecordingDevice(profile)
    return store, device, ControlEngine(store, {"solarbank": device})


@pytest.mark.asyncio
async def test_encodes_select_and_power_direction() -> None:
    state = {
        "operating_mode": "self_consumption",
        "ems_mode_mask": 0b100001,
        "parallel_capability_mask": 1,
        "battery_power_direction": "charge",
        "max_charge_power": 2400,
    }
    store, device, engine = await _engine(state)

    mode = await engine.execute(
        "solarbank", device.profile.metadata.entities["operating_mode"], "third_party_control"
    )
    direction = await engine.execute(
        "solarbank", device.profile.metadata.entities["battery_power_direction"], "charge"
    )
    power = await engine.execute(
        "solarbank", device.profile.metadata.entities["battery_power_setpoint"], 500.0
    )

    assert mode.success and direction.success and power.success
    assert device.writes == [(10064, 3, "UINT16"), (10071, -500, "INT32")]
    assert store.get_snapshot("solarbank").values["battery_power_setpoint"] == 500.0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rejects_unsupported_capability_and_soc_relation() -> None:
    state = {
        "parallel_capability_mask": 0,
        "backup_soc_enable": 1,
        "backup_reserve_soc": 90,
    }
    _store, device, engine = await _engine(state)
    entity = device.profile.metadata.entities["charging_limit_soc"]

    unsupported = await engine.execute("solarbank", entity, 95.0)
    assert not unsupported.success
    assert "not supported" in (unsupported.error or "")

    state["parallel_capability_mask"] = 1
    _store, device, engine = await _engine(state)
    invalid_soc = await engine.execute(
        "solarbank", device.profile.metadata.entities["charging_limit_soc"], 85.0
    )
    assert not invalid_soc.success
    assert "greater than backup_reserve_soc" in (invalid_soc.error or "")
    assert device.writes == []


@pytest.mark.asyncio
async def test_returns_profile_warning_after_successful_write() -> None:
    state = {
        "operating_mode": "third_party_control",
        "battery_power_direction": "discharge",
        "max_discharge_power": 800,
    }
    _store, device, engine = await _engine(state)

    result = await engine.execute(
        "solarbank", device.profile.metadata.entities["battery_power_setpoint"], 50.0
    )

    assert result.success
    assert result.warnings == ("Value is below the optimal power range",)
    assert device.writes == [(10071, 50, "INT32")]
