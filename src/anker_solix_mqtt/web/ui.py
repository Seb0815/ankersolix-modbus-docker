"""Server-rendered operating interface."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import EntityMetadata
from ..state_store import StateStore

CATEGORY_NAMES = {
    "battery": "Battery",
    "power": "Power",
    "energy": "Energy",
    "grid": "Grid",
    "pv": "Solar",
    "temperature": "Temperatures",
    "diagnostic": "Diagnostics",
    "other": "Other values",
}


def create_ui_router(store: StateStore, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    async def _dashboard(request: Request) -> HTMLResponse:
        devices: list[dict[str, Any]] = []
        for metadata in store.list_metadata():
            snapshot = store.get_snapshot(metadata.device_id)
            groups: dict[str, list[EntityMetadata]] = defaultdict(list)
            controls: list[EntityMetadata] = []
            for entity in metadata.entities.values():
                if not entity.visible:
                    continue
                if entity.writable:
                    controls.append(entity)
                else:
                    groups[entity.category].append(entity)
            devices.append(
                {
                    "metadata": metadata,
                    "snapshot": snapshot,
                    "groups": [
                        {
                            "key": key,
                            "name": CATEGORY_NAMES.get(key, key.replace("_", " ").title()),
                            "entities": entities,
                        }
                        for key, entities in groups.items()
                    ],
                    "controls": controls,
                }
            )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "devices": devices,
                "revision": store.revision,
            },
        )

    router.add_api_route("/", _dashboard, methods=["GET"], response_class=HTMLResponse)
    return router
