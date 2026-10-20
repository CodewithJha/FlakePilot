import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_logger = logging.getLogger("flakepilot.http")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def error_body(code: str, message: str, request_id: str | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _client_request_id(scope: Scope) -> str | None:
    """The inbound X-Request-ID, only if there is exactly one and it is safe to log."""
    inbound = [v for k, v in scope["headers"] if k == b"x-request-id"]
    if len(inbound) == 1:
        candidate: str = inbound[0].decode("latin-1")
        if _VALID_REQUEST_ID.fullmatch(candidate):
            return candidate
    return None


class RequestContextMiddleware:
    """Generates the request ID, logs one line per request, and is the 500 boundary.

    The request ID is always ours (uuid4) and is returned in X-Request-ID. A valid inbound
    X-Request-ID is only logged as `client_request_id`; it is never used as our ID.
    Unhandled exceptions are logged with their traceback; the client gets a generic body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        client_request_id = _client_request_id(scope)
        token = request_id_var.set(request_id)
        status = 500
        started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            _logger.exception("unhandled exception")
            if started:
                raise
            response = JSONResponse(
                error_body("internal_error", "Internal server error", request_id), 500
            )
            await response(scope, receive, send_with_request_id)
        finally:
            extra = (
                {"client_request_id": client_request_id} if client_request_id else None
            )
            _logger.info(
                "%s %s %d", scope["method"], scope["path"], status, extra=extra
            )
            request_id_var.reset(token)
