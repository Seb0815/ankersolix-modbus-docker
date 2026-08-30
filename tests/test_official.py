from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from anker_solix_mqtt.device import DeviceSettings
from anker_solix_mqtt.official import (
    OfficialBindings,
    OfficialModbusDriver,
    OfficialWriteResult,
)
from anker_solix_mqtt.profile import build_device_profile


def _config() -> dict[str, Any]:
    return {
        "product_info": {"default_name": "Anker SOLIX Solarbank Max AC"},
        "read_quantities": {
            "battery_soc": {
                "translation_key": "battery_soc",
                "unit": "%",
                "data_type": "UINT16",
            },
            "pv_power": {
                "translation_key": "pv_power",
                "unit": "W",
                "additional_sources": ["third_party_pv_power"],
            },
            "battery_charging_power": {"unit": "W", "power_split_mode": "negative_only"},
            "battery_discharging_power": {"unit": "W", "power_split_mode": "positive_only"},
            "battery_status": {"value_mapping": {0: "standby", 1: "charging"}},
        },
        "write_quantities": {
            "enumeration_selection": {
                "operating_mode": {
                    "control_type": "select",
                    "options": {0: "self_consumption", 3: "third_party_control"},
                }
            }
        },
        "control_items": {
            "charging_limit_soc": {
                "data_type_category": "control",
                "display_type": "input",
                "unit": "%",
                "min_value": 80,
                "max_value": 100,
                "step": 1,
            }
        },
    }


def _data_points() -> dict[str, Any]:
    config = _config()
    return {
        **config["read_quantities"],
        **config["write_quantities"]["enumeration_selection"],
        **config["control_items"],
    }


def _translations() -> dict[str, Any]:
    return {
        "entity": {
            "sensor": {"battery_soc": {"name": "SOC"}},
            "select": {
                "operating_mode": {
                    "name": "Betriebsmodus",
                    "state": {
                        "self_consumption": "Eigenverbrauch",
                        "third_party_control": "Drittanbieter-Steuerung",
                    },
                }
            },
            "number": {"charging_limit_soc": {"name": "Ladeobergrenze"}},
        }
    }


def test_builds_metadata_and_normalizes_official_values() -> None:
    profile = build_device_profile(
        "solarbank", None, _config(), _data_points(), [(10000, 10010, "input")], _translations()
    )

    assert profile.metadata.name == "Anker SOLIX Solarbank Max AC"
    assert profile.metadata.entities["charging_limit_soc"].kind == "number"
    assert profile.metadata.entities["charging_limit_soc"].name == "Ladeobergrenze"
    assert profile.metadata.entities["operating_mode"].options == {
        "self_consumption": "Eigenverbrauch",
        "third_party_control": "Drittanbieter-Steuerung",
    }
    assert profile.normalize(
        {
            "battery_soc": 82,
            "pv_power": 400,
            "third_party_pv_power": 100,
            "battery_charging_power": -300,
            "battery_discharging_power": -300,
            "battery_status": 1,
            "operating_mode": 3,
        }
    ) == {
        "battery_soc": 82,
        "pv_power": 500,
        "third_party_pv_power": 100,
        "battery_charging_power": 300,
        "battery_discharging_power": 0,
        "battery_status": "charging",
        "operating_mode": "third_party_control",
    }


@dataclass
class FakeWriteResult:
    success: bool
    error_reason: str = ""
    is_transient: bool = False


class FakeManager:
    def __init__(self) -> None:
        self.initialized_with: tuple[str, int, str | None] | None = None
        self.closed = False

    def initialize(self, ip_address: str, port: int, device_name: str | None) -> None:
        self.initialized_with = (ip_address, port, device_name)

    async def read_device_pn(self) -> tuple[str, str, str]:
        return "profile-hash", "private-pn", "0x0000"

    async def get_all_data(
        self,
        data_points: dict[str, Any],
        batch_ranges: list[tuple[int, int, str]] | None = None,
        *,
        use_batch_optimization: bool = True,
    ) -> dict[str, Any]:
        assert data_points
        assert batch_ranges == [(10000, 10010, "input")]
        assert use_batch_optimization
        return {"battery_soc": 82}

    async def write_register(self, address: int, value: Any, data_type: str) -> OfficialWriteResult:
        return FakeWriteResult(success=True)

    async def disconnect(self) -> None:
        self.closed = True


class FakeConfigLoader:
    def __init__(self, _hass: object | None) -> None:
        self.loaded_path: str | None = None

    async def load_device_config_by_file_async(self, config_file: str) -> dict[str, Any] | None:
        self.loaded_path = config_file
        return _config()


@pytest.mark.asyncio
async def test_official_driver_uses_pn_profile_and_manager() -> None:
    manager = FakeManager()
    loader = FakeConfigLoader(None)
    bindings = OfficialBindings(
        manager_factory=lambda: manager,
        config_loader_factory=lambda _hass: loader,
        config_parser=lambda _config: (_data_points(), [(10000, 10010, "input")]),
        translations=_translations(),
    )
    driver = OfficialModbusDriver(
        DeviceSettings(device_id="solarbank", host="192.0.2.10", name="Keller"), bindings
    )

    metadata = await driver.prepare()
    values = await driver.read()
    await driver.close()

    assert manager.initialized_with == ("192.0.2.10", 502, "Keller")
    assert loader.loaded_path == "config/profile-hash.yaml"
    assert metadata.name == "Keller"
    assert values["battery_soc"] == 82
    assert manager.closed
