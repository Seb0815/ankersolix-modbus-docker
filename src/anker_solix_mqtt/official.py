"""Adapter around the validated official Anker SOLIX Modbus modules."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from .device import DeviceSettings
from .models import DeviceMetadata
from .profile import DeviceProfile, build_device_profile


class OfficialManager(Protocol):
    def initialize(self, ip_address: str, port: int, device_name: str | None) -> None: ...

    async def read_device_pn(self) -> tuple[str, str, str]: ...

    async def get_all_data(
        self,
        data_points: dict[str, Any],
        batch_ranges: list[tuple[int, int, str]] | None = None,
        *,
        use_batch_optimization: bool = True,
    ) -> dict[str, Any]: ...

    async def write_register(
        self, address: int, value: Any, data_type: str
    ) -> OfficialWriteResult: ...

    async def disconnect(self) -> None: ...


class OfficialWriteResult(Protocol):
    success: bool
    error_reason: str
    is_transient: bool


@dataclass(frozen=True)
class DriverWriteResult:
    success: bool
    error: str | None = None
    transient: bool = False


class OfficialConfigLoader(Protocol):
    async def load_device_config_by_file_async(self, config_file: str) -> dict[str, Any] | None: ...


ManagerFactory = Callable[[], OfficialManager]
ConfigLoaderFactory = Callable[[object | None], OfficialConfigLoader]
ConfigParser = Callable[[dict[str, Any]], tuple[dict[str, Any], list[tuple[int, int, str]]]]


@dataclass(frozen=True)
class OfficialBindings:
    manager_factory: ManagerFactory
    config_loader_factory: ConfigLoaderFactory
    config_parser: ConfigParser
    translations: dict[str, Any]


class UnsupportedDeviceError(RuntimeError):
    """Raised when no official profile exists for the detected product number."""


class OfficialModbusDriver:
    """Use the official Modbus implementation without importing Home Assistant."""

    def __init__(self, settings: DeviceSettings, bindings: OfficialBindings | None = None) -> None:
        self._settings = settings
        self._bindings = bindings or load_official_bindings()
        self._manager = self._bindings.manager_factory()
        self._manager.initialize(settings.host, settings.port, settings.name)
        self._config_loader = self._bindings.config_loader_factory(None)
        self._profile: DeviceProfile | None = None

    @property
    def profile(self) -> DeviceProfile | None:
        return self._profile

    async def prepare(self) -> DeviceMetadata:
        pn_hash, _raw_pn, raw_registers = await self._manager.read_device_pn()
        if not pn_hash:
            raise ConnectionError(f"Could not read device PN ({raw_registers})")
        config = await self._config_loader.load_device_config_by_file_async(
            f"config/{pn_hash}.yaml"
        )
        if config is None:
            raise UnsupportedDeviceError(f"No official profile for PN hash {pn_hash}")
        data_points, batch_ranges = self._bindings.config_parser(config)
        if not data_points:
            raise UnsupportedDeviceError(f"Official profile {pn_hash} contains no data points")
        self._profile = build_device_profile(
            self._settings.device_id,
            self._settings.name,
            config,
            data_points,
            batch_ranges,
            self._bindings.translations,
        )
        return self._profile.metadata

    async def read(self) -> dict[str, Any]:
        if self._profile is None:
            raise RuntimeError("Device has not been prepared")
        values = await self._manager.get_all_data(
            self._profile.data_points,
            batch_ranges=self._profile.batch_ranges,
            use_batch_optimization=True,
        )
        return self._profile.normalize(values) if values else {}

    async def write_raw(self, address: int, value: Any, data_type: str) -> DriverWriteResult:
        result = await self._manager.write_register(address, value, data_type)
        return DriverWriteResult(
            success=result.success,
            error=result.error_reason or None,
            transient=result.is_transient,
        )

    async def close(self) -> None:
        await self._manager.disconnect()


def load_official_bindings() -> OfficialBindings:
    """Load generated upstream modules only when a real driver is constructed."""
    package = "anker_solix_mqtt.vendor.anker_solix_official"
    try:
        manager_module = importlib.import_module(f"{package}.modbus_manager")
        config_module = importlib.import_module(f"{package}.device_config")
        parser_module = importlib.import_module(f"{package}.config_utils")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Official Modbus core is missing; run fetch-anker-solix-upstream first"
        ) from exc

    package_root = Path(cast(str, manager_module.__file__)).parent
    translations = json.loads((package_root / "translations" / "en.json").read_text())
    if not isinstance(translations, dict):
        raise RuntimeError("Invalid English upstream translation file")
    return OfficialBindings(
        manager_factory=cast(ManagerFactory, manager_module.ModbusConnectionManager),
        config_loader_factory=cast(ConfigLoaderFactory, config_module.AnkerSolixDeviceConfig),
        config_parser=cast(ConfigParser, parser_module.parse_device_configuration),
        translations=cast(dict[str, Any], translations),
    )
