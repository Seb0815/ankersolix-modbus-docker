"""Application composition and executable entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .commands import CommandDispatcher
from .control_engine import ControlEngine, ControllableDevice
from .device import DeviceDriver, DeviceRuntime, DeviceSettings
from .mqtt import MQTTAdapter, MQTTSettings, MQTTTransport
from .official import OfficialModbusDriver, load_official_bindings
from .state_store import StateStore
from .web.app import create_web_app

LOGGER = logging.getLogger(__name__)


class WebSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class AppSettings(BaseSettings):
    """Settings loaded from ANKER_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="ANKER_",
        env_nested_delimiter="__",
        frozen=True,
    )

    devices: tuple[DeviceSettings, ...] = Field(min_length=1)
    mqtt: MQTTSettings
    web: WebSettings = WebSettings()
    log_level: str = "INFO"

    @model_validator(mode="after")
    def unique_device_ids(self) -> AppSettings:
        device_ids = [device.device_id for device in self.devices]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("device_id muss eindeutig sein")
        return self


class ApplicationDevice(DeviceDriver, ControllableDevice, Protocol):
    """Combined runtime contract implemented by the official driver."""


DriverFactory = Callable[[DeviceSettings], ApplicationDevice]


class BridgeRuntime:
    """Own MQTT and all device polling tasks for one application process."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        driver_factory: DriverFactory | None = None,
        mqtt_transport: MQTTTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = StateStore()
        if driver_factory is None:
            bindings = load_official_bindings()

            def create_official_driver(device: DeviceSettings) -> ApplicationDevice:
                return OfficialModbusDriver(device, bindings)

            driver_factory = create_official_driver
        self.devices: dict[str, ApplicationDevice] = {
            device.device_id: driver_factory(device) for device in settings.devices
        }
        controllable_devices: Mapping[str, ControllableDevice] = self.devices
        self.control_engine = ControlEngine(self.store, controllable_devices)
        self.dispatcher = CommandDispatcher(self.store, self.control_engine)
        self.mqtt = MQTTAdapter(
            settings.mqtt,
            self.store,
            self.dispatcher,
            mqtt_transport,
        )
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop_event.clear()
        await self.mqtt.start()
        for settings in self.settings.devices:
            runtime = DeviceRuntime(settings, self.store, self.devices[settings.device_id])
            self._tasks.append(
                asyncio.create_task(
                    runtime.run(self._stop_event), name=f"device-{settings.device_id}"
                )
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._tasks:
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    LOGGER.error("Device task stopped with an error", exc_info=result)
            self._tasks.clear()
        await self.mqtt.stop()


def create_application(
    settings: AppSettings,
    *,
    driver_factory: DriverFactory | None = None,
    mqtt_transport: MQTTTransport | None = None,
) -> FastAPI:
    """Compose the runtime and web app around one shared state store."""
    runtime = BridgeRuntime(
        settings,
        driver_factory=driver_factory,
        mqtt_transport=mqtt_transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = create_web_app(runtime.store, runtime.dispatcher, lifespan=lifespan)
    app.state.bridge_runtime = runtime
    return app


def load_settings() -> AppSettings:
    """Load application settings from the ANKER_* environment."""
    return AppSettings()  # pyright: ignore[reportCallIssue]


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_application(settings),
        host=settings.web.host,
        port=settings.web.port,
        log_level=settings.log_level.lower(),
    )
