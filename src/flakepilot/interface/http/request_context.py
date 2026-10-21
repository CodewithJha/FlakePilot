"""Request ID, the error body that carries it, and the per-request middleware.

Error contract (stable): every error response is JSON
`{"error": {"code": <code>, "message": <text>, "request_id": <id>}}`, and the same id is
in the `X-Request-ID` header. `code` is one of the constants below and never changes
meaning; `message` is human-readable and may change. 422 adds `fields`: a list of
`{"loc", "type"}` pairs (Pydantic's input values and messages are never included).
"""

import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Stable error codes, with the HTTP status each is sent with.
UNAUTHENTICATED = "unauthenticated"  # 401: no verified principal
FORBIDDEN = "forbidden"  # 403: resource visible, action not permitted
NOT_FOUND = "not_found"  # 404: no such route, or a resource the principal cannot see
METHOD_NOT_ALLOWED = "method_not_allowed"  # 405, with an Allow header
VALIDATION_ERROR = "validation_error"  # 422: request failed schema validation
HTTP_ERROR = "http_error"  # any other HTTPException status
INTERNAL_ERROR = "internal_error"  # 500: unexpected, database or connection error
AUTH_NOT_CONFIGURED = "auth_not_configured"  # 503: sign-in is not configured
AUTH_PROVIDER_UNAVAILABLE = "auth_provider_unavailable"  # 503: sign-in provider is down

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_LOGGED_PATH = 256
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


def _logged_path(scope: Scope) -> str:
    """The matched route template (no path values); else the raw path, truncated."""
    template: str | None = getattr(scope.get("route"), "path_format", None)
    if template:
        return template
    path: str = scope["path"]
    return path if len(path) <= _MAX_LOGGED_PATH else path[:_MAX_LOGGED_PATH] + "…"


class RequestContextMiddleware:
    """Generates the request ID, logs one line per request, and is the 500 boundary.

    The request ID is always ours (uuid4) and is returned in X-Request-ID. A valid inbound
    X-Request-ID is only logged as `client_request_id`; it is never used as our ID.
    The request line logs the matched route template, so path values (IDs, tokens) and the
    query string never reach the log. The router records the matched route in the shared
    scope, which is read after the app has run.
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
                error_body(INTERNAL_ERROR, "Internal server error", request_id), 500
            )
            await response(scope, receive, send_with_request_id)
        finally:
            extra = (
                {"client_request_id": client_request_id} if client_request_id else None
            )
            _logger.info(
                "%s %s %d", scope["method"], _logged_path(scope), status, extra=extra
            )
            request_id_var.reset(token)
