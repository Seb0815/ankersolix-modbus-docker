"""Shared immutable runtime models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class EntityMetadata(BaseModel):
    """Machine-readable description of a value or control."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    kind: Literal["sensor", "number", "select", "switch"]
    category: str = "other"
    unit: str | None = None
    writable: bool = False
    visible: bool = True
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    options: dict[str, str] = Field(default_factory=dict)


class DeviceMetadata(BaseModel):
    """Metadata used by MQTT, HTTP and the generated UI."""

    model_config = ConfigDict(frozen=True)

    device_id: str
    name: str
    model: str = "Unknown"
    manufacturer: str = "Anker"
    entities: dict[str, EntityMetadata] = Field(default_factory=dict)


class DeviceSnapshot(BaseModel):
    """Current normalized device state."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    device_id: str
    revision: int = 0
    timestamp: datetime = Field(default_factory=utc_now)
    last_successful_poll: datetime | None = None
    online: bool = False
    stale: bool = True
    error: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class StateEvent(BaseModel):
    """Revisioned event distributed to MQTT and browser clients."""

    model_config = ConfigDict(frozen=True)

    event: Literal["state", "metadata", "device_status", "command_result"]
    device_id: str | None
    revision: int
    data: dict[str, Any]


class ControlCommand(BaseModel):
    """Normalized command accepted from MQTT or HTTP."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    device_id: str = Field(min_length=1, max_length=128)
    entity_key: str = Field(min_length=1, max_length=128)
    value: Any
    source: Literal["mqtt", "web"]


class ControlOutcome(BaseModel):
    """Backend outcome before transport-independent result wrapping."""

    model_config = ConfigDict(frozen=True)

    success: bool
    value: Any = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    transient: bool = False


class ControlResult(BaseModel):
    """Transport-independent command result."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    device_id: str
    entity_key: str
    status: Literal["success", "rejected", "error"]
    value: Any = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    transient: bool = False
    timestamp: datetime = Field(default_factory=utc_now)
