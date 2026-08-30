"""Profile-driven command execution shared by MQTT and HTTP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from .models import ControlOutcome, DeviceSnapshot, EntityMetadata
from .official import DriverWriteResult
from .profile import DeviceProfile
from .state_store import StateStore


class ControllableDevice(Protocol):
    @property
    def profile(self) -> DeviceProfile | None: ...

    async def write_raw(self, address: int, value: Any, data_type: str) -> DriverWriteResult: ...


class ControlEngine:
    """Validate official profile rules and serialize writes per device manager."""

    def __init__(self, store: StateStore, devices: Mapping[str, ControllableDevice]) -> None:
        self._store = store
        self._devices = devices

    async def execute(self, device_id: str, entity: EntityMetadata, value: Any) -> ControlOutcome:
        device = self._devices.get(device_id)
        if device is None or device.profile is None:
            return ControlOutcome(success=False, error="Gerät ist nicht bereit", transient=True)
        snapshot = self._store.get_snapshot(device_id)
        if snapshot is None or not snapshot.online:
            return ControlOutcome(success=False, error="Gerät ist offline", transient=True)

        config = device.profile.controls.get(entity.key)
        if config is None:
            return ControlOutcome(success=False, error="Steuerung fehlt im Geräteprofil")
        error = self._validate_profile_rules(device.profile, config, value, snapshot.values)
        if error:
            return ControlOutcome(success=False, error=error)

        if config.get("is_direction_selector"):
            await self._publish_optimistic(snapshot, entity.key, value)
            return ControlOutcome(success=True, value=value)

        try:
            address = int(config["address"])
            data_type = str(config.get("data_type", "UINT16"))
            write_value = self._encode_value(device.profile, entity, config, value, snapshot.values)
        except (KeyError, TypeError, ValueError) as exc:
            return ControlOutcome(success=False, error=f"Ungültiges Geräteprofil: {exc}")

        result = await device.write_raw(address, write_value, data_type)
        if not result.success:
            return ControlOutcome(
                success=False,
                error=result.error or "Modbus-Schreibvorgang fehlgeschlagen",
                transient=result.transient,
            )

        warnings = self._warnings(config, value)
        await self._publish_optimistic(snapshot, entity.key, value)
        return ControlOutcome(success=True, value=value, warnings=warnings)

    def _validate_profile_rules(
        self,
        profile: DeviceProfile,
        config: dict[str, Any],
        value: Any,
        state: dict[str, Any],
    ) -> str | None:
        capability_entity = config.get("capability_entity")
        capability_bit = config.get("capability_bit")
        if isinstance(capability_entity, str) and isinstance(capability_bit, int):
            mask = _integer(state.get(capability_entity))
            if mask is None or not mask & (1 << capability_bit):
                return "Steuerung wird von diesem Gerät nicht unterstützt"

        visibility_entity = config.get("visibility_entity")
        if isinstance(visibility_entity, str):
            expected = self._semantic_value(
                profile, visibility_entity, config.get("visibility_value")
            )
            if state.get(visibility_entity) != expected:
                return "Steuerung ist im aktuellen Betriebsmodus nicht verfügbar"

        condition = _mapping(config.get("write_condition"))
        if condition:
            condition_entity = condition.get("entity")
            if not isinstance(condition_entity, str) or not _compare(
                state.get(condition_entity), condition.get("operator"), condition.get("value")
            ):
                if condition.get("hint") == "enable_backup_soc_first":
                    return "Zuerst muss die Notstromreserve aktiviert werden"
                return "Voraussetzung für diese Steuerung ist nicht erfüllt"

        soc_validation = _mapping(config.get("soc_validation"))
        if soc_validation and _condition_applies(soc_validation, state):
            numeric_value = _float(value)
            if numeric_value is None:
                return "Zahlenwert erwartet"
            error = _validate_soc(numeric_value, soc_validation, state)
            if error:
                return error

        direction_entity = config.get("direction_entity")
        if isinstance(direction_entity, str):
            direction = state.get(direction_entity)
            if direction not in {"charge", "discharge"}:
                return "Zuerst Lade- oder Entladerichtung auswählen"
            limit_key = (
                config.get("max_charge_power_entity")
                if direction == "charge"
                else config.get("max_discharge_power_entity")
            )
            if isinstance(limit_key, str):
                limit = _float(state.get(limit_key))
                numeric_value = _float(value)
                if limit is not None and numeric_value is not None and numeric_value > abs(limit):
                    return f"Wert darf für diese Richtung höchstens {abs(limit):g} sein"

        if entity_options := config.get("option_capability_bits"):
            option_bits = _mapping(entity_options)
            raw_option = _raw_option(config, value)
            bit = _integer(_mapped(option_bits, raw_option))
            capability_key = config.get("capability_entity")
            mask = _integer(state.get(capability_key)) if isinstance(capability_key, str) else None
            if bit is not None and (mask is None or not mask & (1 << bit)):
                return "Auswahl wird von diesem Gerät nicht unterstützt"
        return None

    @staticmethod
    def _semantic_value(profile: DeviceProfile, entity_key: str, raw_value: object) -> object:
        target = profile.controls.get(entity_key)
        if target is None:
            return raw_value
        return _mapped(_mapping(target.get("options")), raw_value, raw_value)

    @staticmethod
    def _encode_value(
        profile: DeviceProfile,
        entity: EntityMetadata,
        config: dict[str, Any],
        value: Any,
        state: dict[str, Any],
    ) -> Any:
        if entity.kind == "select":
            raw_option = _integer(_raw_option(config, value))
            if raw_option is None:
                raise ValueError("Auswahlcode ist keine Zahl")
            return raw_option
        if entity.kind == "switch":
            semantic = "enabled" if value else "disabled"
            raw_value = _raw_for_semantic(_mapping(config.get("options")), semantic)
            encoded = _integer(raw_value) if raw_value is not None else int(bool(value))
            if encoded is None:
                raise ValueError("Schaltercode ist keine Zahl")
            return encoded

        numeric_value = float(value)
        gain = _float(config.get("gain")) or 1
        write_value = numeric_value * gain
        direction_entity = config.get("direction_entity")
        if isinstance(direction_entity, str) and state.get(direction_entity) == "charge":
            write_value = -abs(write_value)
        elif isinstance(direction_entity, str):
            write_value = abs(write_value)
        return int(write_value) if write_value.is_integer() else write_value

    @staticmethod
    def _warnings(config: dict[str, Any], value: Any) -> tuple[str, ...]:
        numeric_value = _float(value)
        if numeric_value is None:
            return ()
        rules = _mapping(config.get("value_constraints")).get("rules")
        if not isinstance(rules, list):
            return ()
        for item in cast(list[object], rules):
            rule = _mapping(item)
            minimum = _float(rule.get("min"))
            maximum = _float(rule.get("max"))
            if (
                rule.get("type") == "warning_range"
                and minimum is not None
                and maximum is not None
                and minimum <= numeric_value <= maximum
            ):
                return ("Wert liegt unter dem optimalen Leistungsbereich",)
        return ()

    async def _publish_optimistic(
        self, snapshot: DeviceSnapshot, entity_key: str, value: Any
    ) -> None:
        values = dict(snapshot.values)
        values[entity_key] = value
        await self._store.publish_snapshot(snapshot.model_copy(update={"values": values}))


def _validate_soc(value: float, config: dict[str, Any], state: dict[str, Any]) -> str | None:
    comparisons: dict[str, tuple[Callable[[float, float], bool], str]] = {
        "greater_than": (lambda left, right: left > right, "größer als"),
        "greater_than_or_equal": (lambda left, right: left >= right, "größer oder gleich"),
        "less_than": (lambda left, right: left < right, "kleiner als"),
        "less_than_or_equal": (lambda left, right: left <= right, "kleiner oder gleich"),
    }
    for rule_name, (comparison, description) in comparisons.items():
        targets = config.get(rule_name)
        if targets is None:
            continue
        target_names = [targets] if isinstance(targets, str) else targets
        if not isinstance(target_names, list):
            continue
        for target_name in cast(list[object], target_names):
            if not isinstance(target_name, str):
                continue
            target_value = _float(state.get(target_name))
            if target_value is not None and not comparison(value, target_value):
                return f"Wert muss {description} {target_name} ({target_value:g}) sein"
    return None


def _condition_applies(config: dict[str, Any], state: dict[str, Any]) -> bool:
    entity = config.get("condition_entity")
    if not isinstance(entity, str):
        return True
    return _compare(state.get(entity), "eq", config.get("condition_value"))


def _compare(left: object, operator: object, right: object) -> bool:
    if operator == "eq":
        return left == right or str(left) == str(right)
    left_number = _float(left)
    right_number = _float(right)
    if left_number is None or right_number is None:
        return False
    return {
        "ne": left_number != right_number,
        "gt": left_number > right_number,
        "ge": left_number >= right_number,
        "lt": left_number < right_number,
        "le": left_number <= right_number,
    }.get(str(operator), False)


def _raw_option(config: dict[str, Any], semantic: object) -> object:
    raw_value = _raw_for_semantic(_mapping(config.get("options")), semantic)
    if raw_value is None:
        raise ValueError(f"unbekannte Auswahl {semantic!r}")
    return raw_value


def _raw_for_semantic(options: dict[str, Any], semantic: object) -> object | None:
    for raw_value, option in options.items():
        if option == semantic:
            return raw_value
    return None


def _mapped(mapping: dict[str, Any], key: object, default: Any = None) -> Any:
    expected = str(key)
    for candidate, value in mapping.items():
        if str(candidate) == expected:
            return value
    return default


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None
