"""Single validated command path shared by MQTT and HTTP."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .models import (
    ControlCommand,
    ControlOutcome,
    ControlResult,
    EntityMetadata,
)
from .state_store import StateStore


class ControlBackend(Protocol):
    """Device-specific writer called only after generic validation."""

    async def execute(
        self, device_id: str, entity: EntityMetadata, value: Any
    ) -> ControlOutcome: ...


class CommandDispatcher:
    """Validate, deduplicate and execute commands through one backend."""

    def __init__(self, store: StateStore, backend: ControlBackend) -> None:
        self._store = store
        self._backend = backend
        self._results: dict[str, ControlResult] = {}
        self._lock = asyncio.Lock()

    async def dispatch(self, command: ControlCommand) -> ControlResult:
        """Return the prior result for duplicate request IDs."""
        async with self._lock:
            if cached := self._results.get(command.request_id):
                return cached

            entity, error = self._find_entity(command)
            if error:
                result = self._result(command, "rejected", error=error)
            else:
                assert entity is not None
                value, error = self._normalize_value(entity, command.value)
                if error:
                    result = self._result(command, "rejected", error=error)
                else:
                    try:
                        outcome = await self._backend.execute(command.device_id, entity, value)
                    except Exception as exc:
                        result = self._result(command, "error", error=str(exc), transient=True)
                    else:
                        result = self._from_outcome(command, outcome)

            self._results[command.request_id] = result
            await self._store.publish_event(
                "command_result",
                command.device_id,
                result.model_dump(mode="json"),
            )
            return result

    def _find_entity(self, command: ControlCommand) -> tuple[EntityMetadata | None, str | None]:
        metadata = self._store.get_metadata(command.device_id)
        if metadata is None:
            return None, "Unbekanntes Gerät"
        entity = metadata.entities.get(command.entity_key)
        if entity is None:
            return None, "Unbekannte Steuerung"
        if not entity.writable:
            return None, "Entität ist schreibgeschützt"
        if not entity.visible:
            return None, "Steuerung ist für dieses Gerät nicht verfügbar"
        return entity, None

    @staticmethod
    def _normalize_value(entity: EntityMetadata, value: Any) -> tuple[Any, str | None]:
        if entity.kind == "number":
            if isinstance(value, bool):
                return None, "Zahlenwert erwartet"
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None, "Zahlenwert erwartet"
            if entity.min_value is not None and number < entity.min_value:
                return None, f"Wert muss mindestens {entity.min_value:g} sein"
            if entity.max_value is not None and number > entity.max_value:
                return None, f"Wert darf höchstens {entity.max_value:g} sein"
            return number, None

        if entity.kind == "select":
            option = str(value)
            if option not in entity.options:
                return None, "Ungültige Auswahl"
            return option, None

        if entity.kind == "switch":
            if not isinstance(value, bool):
                return None, "Boolescher Wert erwartet"
            return value, None

        return None, "Sensoren können nicht geschrieben werden"

    @staticmethod
    def _result(
        command: ControlCommand,
        status: str,
        *,
        value: Any = None,
        warnings: tuple[str, ...] = (),
        error: str | None = None,
        transient: bool = False,
    ) -> ControlResult:
        return ControlResult.model_validate(
            {
                "request_id": command.request_id,
                "device_id": command.device_id,
                "entity_key": command.entity_key,
                "status": status,
                "value": value,
                "warnings": warnings,
                "error": error,
                "transient": transient,
            }
        )

    def _from_outcome(self, command: ControlCommand, outcome: ControlOutcome) -> ControlResult:
        return self._result(
            command,
            "success" if outcome.success else "error",
            value=outcome.value,
            warnings=outcome.warnings,
            error=outcome.error,
            transient=outcome.transient,
        )
