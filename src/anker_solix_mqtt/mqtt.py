"""MQTT transport and adapter for state publication and commands."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .commands import CommandDispatcher
from .models import ControlCommand, StateEvent
from .state_store import StateStore

LOGGER = logging.getLogger(__name__)


class MQTTSettings(BaseModel):
    """Connection and topic settings for one MQTT broker."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    client_id: str = "anker-solix-mqtt"
    topic_prefix: str = Field(
        default="anker-solix",
        pattern=r"^[^/#+\s]+(?:/[^/#+\s]+)*$",
    )
    qos: Literal[0, 1, 2] = 1
    keepalive: int = Field(default=60, ge=5, le=65535)
    tls: bool = False
    offline_queue_size: int = Field(default=128, ge=1)

    @field_validator("username", "password", mode="before")
    @classmethod
    def empty_credentials_are_unset(cls, value: object) -> object:
        return None if value == "" else value


@dataclass(frozen=True, slots=True)
class MQTTPublication:
    topic: str
    payload: str
    qos: int
    retain: bool


class MQTTTransport(Protocol):
    """Minimal broker transport used by the asyncio adapter."""

    def set_callbacks(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes], None],
    ) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def subscribe(self, topic: str, qos: int) -> None: ...

    def publish(self, publication: MQTTPublication) -> None: ...


class _TLSClient(Protocol):
    def tls_set_context(self, context: ssl.SSLContext) -> None: ...


class PahoMQTTTransport:
    """Threaded Paho client hidden behind a small synchronous boundary."""

    def __init__(self, settings: MQTTSettings) -> None:
        self._settings = settings
        self._client = paho.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=settings.client_id,
        )
        self._on_connected: Callable[[], None] = lambda: None
        self._on_disconnected: Callable[[], None] = lambda: None
        self._on_received: Callable[[str, bytes], None] = lambda _topic, _payload: None

        if settings.username is not None:
            self._client.username_pw_set(settings.username, settings.password)
        if settings.tls:
            cast(_TLSClient, self._client).tls_set_context(ssl.create_default_context())
        self._client.will_set(
            f"{settings.topic_prefix}/bridge/availability",
            "offline",
            qos=settings.qos,
            retain=True,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    def set_callbacks(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes], None],
    ) -> None:
        self._on_connected = on_connect
        self._on_disconnected = on_disconnect
        self._on_received = on_message

    def start(self) -> None:
        self._client.connect_async(
            self._settings.host,
            self._settings.port,
            self._settings.keepalive,
        )
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def subscribe(self, topic: str, qos: int) -> None:
        result, _message_id = self._client.subscribe(topic, qos=qos)
        if result != paho.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT subscribe failed with code {result}")

    def publish(self, publication: MQTTPublication) -> None:
        result = self._client.publish(
            publication.topic,
            publication.payload,
            qos=publication.qos,
            retain=publication.retain,
        )
        if result.rc != paho.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with code {result.rc}")

    def _handle_connect(
        self,
        _client: paho.Client,
        _userdata: Any,
        _flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection rejected: %s", reason_code)
            return
        self._on_connected()

    def _handle_disconnect(
        self,
        _client: paho.Client,
        _userdata: Any,
        _flags: paho.DisconnectFlags,
        _reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        self._on_disconnected()

    def _handle_message(
        self,
        _client: paho.Client,
        _userdata: Any,
        message: paho.MQTTMessage,
    ) -> None:
        self._on_received(message.topic, message.payload)


class MQTTAdapter:
    """Bridge state events and commands without exposing Modbus to MQTT."""

    def __init__(
        self,
        settings: MQTTSettings,
        store: StateStore,
        dispatcher: CommandDispatcher,
        transport: MQTTTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._dispatcher = dispatcher
        self._transport = transport or PahoMQTTTransport(settings)
        self._transport.set_callbacks(
            self._on_transport_connected,
            self._on_transport_disconnected,
            self._on_transport_message,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._subscriber_ready = asyncio.Event()
        self._connected = False
        self._retained: dict[str, MQTTPublication] = {}
        self._transient: deque[MQTTPublication] = deque(maxlen=settings.offline_queue_size)

    async def start(self) -> None:
        """Subscribe to the store before the broker can deliver messages."""
        if self._event_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._event_task = asyncio.create_task(self._consume_events(), name="mqtt-store-events")
        await self._subscriber_ready.wait()
        self._transport.start()

    async def stop(self) -> None:
        """Publish graceful bridge shutdown and stop all MQTT activity."""
        if self._connected:
            self._transport.publish(
                self._publication("bridge/availability", "offline", retain=True)
            )
        self._connected = False
        self._transport.stop()
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
            self._event_task = None
        self._subscriber_ready.clear()
        self._loop = None

    async def _consume_events(self) -> None:
        async with self._store.subscribe() as queue:
            self._subscriber_ready.set()
            while True:
                event = await queue.get()
                await self._publish_event(event)

    async def _publish_event(self, event: StateEvent) -> None:
        if event.device_id is None:
            return
        if event.event == "state":
            self._send_or_buffer(
                self._json_publication(f"{event.device_id}/state", event.data, retain=True)
            )
            availability = "online" if event.data.get("online") is True else "offline"
            self._send_or_buffer(
                self._publication(f"{event.device_id}/availability", availability, retain=True)
            )
        elif event.event == "metadata":
            self._send_or_buffer(
                self._json_publication(f"{event.device_id}/metadata", event.data, retain=True)
            )
        elif event.event == "command_result":
            self._send_or_buffer(
                self._json_publication(
                    f"{event.device_id}/command/result", event.data, retain=False
                )
            )

    def _on_transport_connected(self) -> None:
        self._schedule(self._handle_connected)

    def _on_transport_disconnected(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._mark_disconnected)

    def _on_transport_message(self, topic: str, payload: bytes) -> None:
        self._schedule(lambda: self._handle_command(topic, payload))

    def _schedule(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(factory()))

    def _mark_disconnected(self) -> None:
        self._connected = False

    async def _handle_connected(self) -> None:
        self._transport.subscribe(f"{self._settings.topic_prefix}/+/command", self._settings.qos)
        self._send_or_buffer(self._publication("bridge/availability", "online", retain=True))

        for metadata in self._store.list_metadata():
            self._send_or_buffer(
                self._json_publication(
                    f"{metadata.device_id}/metadata",
                    metadata.model_dump(mode="json"),
                    retain=True,
                )
            )
        for snapshot in self._store.list_snapshots():
            self._send_or_buffer(
                self._json_publication(
                    f"{snapshot.device_id}/state",
                    snapshot.model_dump(mode="json"),
                    retain=True,
                )
            )
            availability = "online" if snapshot.online else "offline"
            self._send_or_buffer(
                self._publication(f"{snapshot.device_id}/availability", availability, retain=True)
            )

        pending = [*self._retained.values(), *self._transient]
        self._retained.clear()
        self._transient.clear()
        self._connected = True
        for publication in pending:
            self._send_or_buffer(publication)

    async def _handle_command(self, topic: str, payload: bytes) -> None:
        device_id = self._command_device_id(topic)
        if device_id is None:
            return
        try:
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("JSON object expected")
            command = ControlCommand.model_validate(
                {**decoded, "device_id": device_id, "source": "mqtt"}
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            self._send_or_buffer(
                self._json_publication(
                    f"{device_id}/command/result",
                    {"status": "rejected", "error": f"Invalid MQTT command: {exc}"},
                    retain=False,
                )
            )
            return
        await self._dispatcher.dispatch(command)

    def _command_device_id(self, topic: str) -> str | None:
        prefix = f"{self._settings.topic_prefix}/"
        suffix = "/command"
        if not topic.startswith(prefix) or not topic.endswith(suffix):
            return None
        device_id = topic[len(prefix) : -len(suffix)]
        return device_id if device_id and "/" not in device_id else None

    def _send_or_buffer(self, publication: MQTTPublication) -> None:
        if self._connected:
            try:
                self._transport.publish(publication)
                return
            except RuntimeError:
                LOGGER.exception("MQTT publish failed; buffering publication")
                self._connected = False
        if publication.retain:
            self._retained[publication.topic] = publication
        else:
            self._transient.append(publication)

    def _publication(self, suffix: str, payload: str, *, retain: bool) -> MQTTPublication:
        return MQTTPublication(
            topic=f"{self._settings.topic_prefix}/{suffix}",
            payload=payload,
            qos=self._settings.qos,
            retain=retain,
        )

    def _json_publication(
        self, suffix: str, payload: dict[str, Any], *, retain: bool
    ) -> MQTTPublication:
        return self._publication(
            suffix,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            retain=retain,
        )
