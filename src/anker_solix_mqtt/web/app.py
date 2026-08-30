"""FastAPI application factory."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..commands import CommandDispatcher
from ..state_store import StateStore
from .api import create_api_router
from .security import BrowserSecurityMiddleware
from .ui import create_ui_router


def create_web_app(
    store: StateStore,
    dispatcher: CommandDispatcher,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Create the embedded API without owning runtime services."""
    app = FastAPI(
        title="Anker SOLIX MQTT Bridge",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    web_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=web_root / "templates")
    app.add_middleware(BrowserSecurityMiddleware)
    app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")
    app.include_router(create_ui_router(store, templates))
    app.include_router(create_api_router(store, dispatcher))

    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "revision": store.revision,
            "devices": len(store.list_metadata()),
        }

    app.add_api_route("/healthz", health, methods=["GET"], include_in_schema=False)
    return app
