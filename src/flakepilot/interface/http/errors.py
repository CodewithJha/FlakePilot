"""Maps framework and application errors to the stable error contract (`request_context`).

Expected failures from use cases: application layers raise plain exception classes defined
in their own context (no FastAPI, no HTTP), and this module maps each class to a status
and a stable code. Bodies never include exception text.

Access failures: authentication failure is 401 `unauthenticated`; a resource the
principal cannot see is 404 `not_found`, the same body as an unknown route, so denials
never reveal whether something exists in another org; a visible resource with a missing
grant is 403 `forbidden`.
"""

import re
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from flakepilot.diagnosis.application.brief import SpokenBriefUnavailable
from flakepilot.execution.application.reproduction import InvalidExecution
from flakepilot.identity.application.authorization import (
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import (
    AuthNotConfigured,
    AuthProviderUnavailable,
    SignInFailed,
)
from flakepilot.interface.http.request_context import (
    AUTH_NOT_CONFIGURED,
    AUTH_PROVIDER_UNAVAILABLE,
    FORBIDDEN,
    HTTP_ERROR,
    IDEMPOTENCY_CONFLICT,
    INGESTION_NOT_CONFIGURED,
    INVALID_REPORT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    SPEECH_NOT_CONFIGURED,
    SPEECH_UNAVAILABLE,
    UNAUTHENTICATED,
    UNSUPPORTED_MEDIA_TYPE,
    VALIDATION_ERROR,
    error_body,
    request_id_var,
)
from flakepilot.test_intelligence.application import ingestion


class Unauthenticated(Exception):
    """Raised by the interface layer when a request has no verified principal."""


class CrossSiteRequest(Exception):
    """A cookie-authenticated unsafe request whose Origin/Referer is not ours (CSRF)."""


class SpeechNotConfigured(Exception):
    """No speech provider is configured; the brief stays text-only."""


_HTTP_CODES = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}
_ACCESS_ERRORS: dict[type[Exception], tuple[int, str]] = {
    Unauthenticated: (401, UNAUTHENTICATED),
    SignInFailed: (401, UNAUTHENTICATED),
    PermissionDenied: (403, FORBIDDEN),
    CrossSiteRequest: (403, FORBIDDEN),
    ResourceNotFound: (404, NOT_FOUND),
    AuthNotConfigured: (503, AUTH_NOT_CONFIGURED),
    AuthProviderUnavailable: (503, AUTH_PROVIDER_UNAVAILABLE),
    ingestion.IngestionNotConfigured: (503, INGESTION_NOT_CONFIGURED),
    SpeechNotConfigured: (503, SPEECH_NOT_CONFIGURED),
}
_REASON = re.compile(r"[a-z][a-z0-9_]{0,39}")
_UPLOAD_REJECTIONS = {
    ingestion.TOO_LARGE: (413, PAYLOAD_TOO_LARGE),
    ingestion.UNSUPPORTED_MEDIA_TYPE: (415, UNSUPPORTED_MEDIA_TYPE),
    ingestion.IDEMPOTENCY_CONFLICT: (409, IDEMPOTENCY_CONFLICT),
}


def access_error_body(exc: Exception) -> tuple[int, dict[str, object]]:
    """Status and body for a mapped access error (for routes that build responses)."""
    status, code = _ACCESS_ERRORS[type(exc)]
    phrase = HTTPStatus(status).phrase
    return status, error_body(code, phrase, request_id_var.get())


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    code = _HTTP_CODES.get(exc.status_code, HTTP_ERROR)
    phrase = HTTPStatus(exc.status_code).phrase
    message = exc.detail if isinstance(exc.detail, str) else phrase
    return JSONResponse(
        error_body(code, message, request_id_var.get()),
        exc.status_code,
        headers=exc.headers,
    )


async def _access_error(request: Request, exc: Exception) -> JSONResponse:
    status, body = access_error_body(exc)
    return JSONResponse(body, status)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 with field locations and error types only.

    Pydantic's `input`, `ctx` and `msg` can echo client-supplied values, so they are
    dropped. For unexpected fields the last `loc` element is the client's own key, so it
    is dropped too.
    """
    assert isinstance(exc, RequestValidationError)
    body = error_body(
        VALIDATION_ERROR, "Request validation failed", request_id_var.get()
    )
    body["error"]["fields"] = [
        {
            "loc": e["loc"][:-1] if e["type"] == "extra_forbidden" else e["loc"],
            "type": e["type"],
        }
        for e in exc.errors()
    ]
    return JSONResponse(body, 422)


async def _upload_rejected(request: Request, exc: Exception) -> JSONResponse:
    """Reasons are fixed codes; submitted values (headers, report content) never appear."""
    assert isinstance(exc, ingestion.UploadRejected)
    request_id = request_id_var.get()
    if exc.reason == ingestion.INVALID_METADATA:
        body = error_body(VALIDATION_ERROR, "Invalid upload metadata", request_id)
        body["error"]["fields"] = [{"loc": ["header", exc.header], "type": "invalid"}]
        return JSONResponse(body, 422)
    status, code = _UPLOAD_REJECTIONS.get(exc.reason, (422, INVALID_REPORT))
    body = error_body(code, HTTPStatus(status).phrase, request_id)
    if code == INVALID_REPORT:
        body["error"]["reason"] = exc.reason
    return JSONResponse(body, status)


async def _invalid_execution(request: Request, exc: Exception) -> JSONResponse:
    """A stable code. The selector, SHA and exception text are not copied."""
    assert isinstance(exc, InvalidExecution)
    reason = exc.reason if _REASON.fullmatch(exc.reason) else "invalid_request"
    body = error_body(
        VALIDATION_ERROR, "Request validation failed", request_id_var.get()
    )
    body["error"]["reason"] = reason
    return JSONResponse(body, 422)


async def _speech_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """The provider's short code only; its text never reaches the client."""
    assert isinstance(exc, SpokenBriefUnavailable)
    body = error_body(SPEECH_UNAVAILABLE, "Service Unavailable", request_id_var.get())
    body["error"]["reason"] = (
        exc.reason if _REASON.fullmatch(exc.reason) else "unavailable"
    )
    return JSONResponse(body, 503)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(SpokenBriefUnavailable, _speech_unavailable)
    app.add_exception_handler(ingestion.UploadRejected, _upload_rejected)
    app.add_exception_handler(InvalidExecution, _invalid_execution)
    for error in _ACCESS_ERRORS:
        app.add_exception_handler(error, _access_error)
