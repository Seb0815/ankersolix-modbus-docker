"""Translate official device profiles into adapter-neutral metadata and values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from .models import DeviceMetadata, EntityMetadata

EntityKind = Literal["sensor", "number", "select", "switch"]


@dataclass(frozen=True)
class DeviceProfile:
    """Parsed official profile used by polling and command execution."""

    metadata: DeviceMetadata
    data_points: dict[str, Any]
    batch_ranges: list[tuple[int, int, str]]
    controls: dict[str, dict[str, Any]]

    def normalize(self, raw_values: dict[str, Any]) -> dict[str, Any]:
        """Apply presentation semantics encoded in the official YAML profile."""
        values = dict(raw_values)
        for key, raw_config in self.data_points.items():
            config = _mapping(raw_config)
            if key not in raw_values:
                if config.get("never_read_device"):
                    values[key] = config.get("default_value", 0)
                continue

            value = raw_values[key]
            sources = config.get("additional_sources")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isinstance(sources, list)
            ):
                value = float(value)
                for source in cast(list[object], sources):
                    source_value = raw_values.get(source) if isinstance(source, str) else None
                    if isinstance(source_value, (int, float)) and not isinstance(
                        source_value, bool
                    ):
                        value += source_value
                if value.is_integer():
                    value = int(value)

            mapping = _mapping(config.get("value_mapping"))
            if mapping and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = _mapped_value(mapping, int(value), value)

            split_mode = config.get("power_split_mode")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if split_mode == "positive_only":
                    value = value if value > 0 else 0
                elif split_mode == "negative_only":
                    value = abs(value) if value < 0 else 0

            values[key] = value

        for key, config in self.controls.items():
            source_key = config.get("read_entity_key", key)
            value = values.get(source_key) if isinstance(source_key, str) else None
            kind = self.metadata.entities[key].kind
            if kind == "select":
                if config.get("is_direction_selector"):
                    values.setdefault(key, "charge")
                    continue
                options = _mapping(config.get("options"))
                if value is not None:
                    option = _mapped_value(options, value)
                    values[key] = option if option is not None else value
            elif kind == "switch" and value is not None:
                options = _mapping(config.get("options"))
                semantic = _mapped_value(options, value)
                values[key] = (
                    semantic in {"enabled", "power_on", "connected"} if options else bool(value)
                )
            elif kind == "number" and isinstance(value, (int, float)):
                if config.get("direction_entity"):
                    values[key] = abs(value)
        return values


def build_device_profile(
    device_id: str,
    configured_name: str | None,
    full_config: dict[str, Any],
    data_points: dict[str, Any],
    batch_ranges: list[tuple[int, int, str]],
    translations: dict[str, Any],
) -> DeviceProfile:
    """Build shared metadata while retaining raw control configuration."""
    product_info = _mapping(full_config.get("product_info"))
    model = _string(product_info.get("default_name")) or "Unknown"
    controls = _collect_controls(full_config)
    entities: dict[str, EntityMetadata] = {}

    for key, raw_config in data_points.items():
        config = _mapping(raw_config)
        kind = _control_kind(config) if key in controls else "sensor"
        translation_key = _string(config.get("translation_key")) or key
        options = _translated_options(config, kind, translation_key, translations)
        entities[key] = EntityMetadata(
            key=key,
            name=_translated_name(translations, kind, translation_key),
            kind=kind,
            category=_category(key, _string(config.get("unit"))),
            unit=_unit(config.get("unit")),
            writable=key in controls,
            visible=not bool(config.get("internal")),
            min_value=_number(config.get("min_value")),
            max_value=_number(config.get("max_value")),
            step=_number(config.get("step")),
            options=options,
        )

    return DeviceProfile(
        metadata=DeviceMetadata(
            device_id=device_id,
            name=configured_name or model,
            model=model,
            entities=entities,
        ),
        data_points=data_points,
        batch_ranges=batch_ranges,
        controls=controls,
    )


def _collect_controls(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    controls: dict[str, dict[str, Any]] = {}
    for key, value in _mapping(config.get("control_items")).items():
        if isinstance(value, dict):
            controls[key] = cast(dict[str, Any], value)
    for group in _mapping(config.get("write_quantities")).values():
        if not isinstance(group, dict):
            continue
        for key, value in cast(dict[str, object], group).items():
            if isinstance(value, dict):
                controls[key] = cast(dict[str, Any], value)
    for key, value in _mapping(config.get("controls")).items():
        if isinstance(value, dict):
            controls[key] = cast(dict[str, Any], value)
    return controls


def _control_kind(config: dict[str, Any]) -> EntityKind:
    control_type = config.get("control_type")
    if control_type in {"select", "switch"}:
        return cast(Literal["select", "switch"], control_type)
    return "number"


def _translated_name(translations: dict[str, Any], kind: EntityKind, translation_key: str) -> str:
    entry = _translation_entry(translations, kind, translation_key)
    return _string(entry.get("name")) or translation_key.replace("_", " ").title()


def _translated_options(
    config: dict[str, Any],
    kind: EntityKind,
    translation_key: str,
    translations: dict[str, Any],
) -> dict[str, str]:
    if kind != "select":
        return {}
    states = _mapping(_translation_entry(translations, kind, translation_key).get("state"))
    result: dict[str, str] = {}
    for semantic in _mapping(config.get("options")).values():
        if isinstance(semantic, str):
            result[semantic] = _string(states.get(semantic)) or semantic.replace("_", " ").title()
    return result


def _translation_entry(
    translations: dict[str, Any], kind: EntityKind, translation_key: str
) -> dict[str, Any]:
    entity = _mapping(translations.get("entity"))
    domain = _mapping(entity.get(kind))
    return _mapping(domain.get(translation_key))


def _category(key: str, unit: str | None) -> str:
    if unit in {"kWh", "Wh"} or any(word in key for word in ("energy", "generation")):
        return "energy"
    if "pv" in key or "solar" in key:
        return "pv"
    if "grid" in key or "meter" in key:
        return "grid"
    if "battery" in key or "soc" in key or "soh" in key:
        return "battery"
    if unit in {"°C", "C"} or "temperature" in key:
        return "temperature"
    if unit in {"W", "kW"} or "power" in key:
        return "power"
    if any(word in key for word in ("model", "version", "status", "sn", "mask")):
        return "diagnostic"
    return "other"


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _mapped_value(mapping: dict[str, Any], key: object, default: Any = None) -> Any:
    expected = str(key)
    for candidate, value in mapping.items():
        if str(candidate) == expected:
            return value
    return default


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _unit(value: object) -> str | None:
    unit = _string(value)
    return None if unit in {None, "/"} else unit
