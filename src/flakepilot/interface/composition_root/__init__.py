"""Composition root: the only place that wires the application together."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version

from fastapi import FastAPI

from flakepilot.interface.composition_root.settings import Settings, SettingsError
from flakepilot.interface.http import errors, health
from flakepilot.interface.http.request_context import (
    RequestContextMiddleware,
    request_id_var,
)

__all__ = ["Settings", "SettingsError", "create_app"]

_HANDLER_NAME = "flakepilot-json"


class JsonFormatter(logging.Formatter):
    """One JSON object per line with a fixed set of fields; request data is never added.

    Optional fields: `client_request_id` (validated inbound X-Request-ID, request log line
    only) and `exception` (server-side tracebacks).
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        client_request_id = getattr(record, "client_request_id", None)
        if client_request_id:
            entry["client_request_id"] = client_request_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str) -> None:
    """Install the JSON handler on the root logger, replacing a previous one."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

    # Interactive docs are off: they load third-party scripts from a CDN.
    # The code-first schema stays available at /openapi.json (ADR-007).
    app = FastAPI(
        title="FlakePilot",
        version=version("flakepilot"),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.ready = False
    app.add_middleware(RequestContextMiddleware)
    errors.register_error_handlers(app)
    app.include_router(health.router)
    return app
