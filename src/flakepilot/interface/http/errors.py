"""Maps framework and application errors to the stable error contract (`request_context`).

Expected failures from use cases: application layers raise plain exception classes defined
in their own context (no FastAPI, no HTTP), and this module maps each class to a status
and a stable code. Bodies never include exception text.

Access failures: authentication failure is 401 `unauthenticated`; a resource the
principal cannot see is 404 `not_found`, the same body as an unknown route, so denials
never reveal whether something exists in another org; a visible resource with a missing
grant is 403 `forbidden`.
"""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

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
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    UNAUTHENTICATED,
    VALIDATION_ERROR,
    error_body,
    request_id_var,
)


class Unauthenticated(Exception):
    """Raised by the interface layer when a request has no verified principal."""


class CrossSiteRequest(Exception):
    """A cookie-authenticated unsafe request whose Origin/Referer is not ours (CSRF)."""


_HTTP_CODES = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}
_ACCESS_ERRORS: dict[type[Exception], tuple[int, str]] = {
    Unauthenticated: (401, UNAUTHENTICATED),
    SignInFailed: (401, UNAUTHENTICATED),
    PermissionDenied: (403, FORBIDDEN),
    CrossSiteRequest: (403, FORBIDDEN),
    ResourceNotFound: (404, NOT_FOUND),
    AuthNotConfigured: (503, AUTH_NOT_CONFIGURED),
    AuthProviderUnavailable: (503, AUTH_PROVIDER_UNAVAILABLE),
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


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    for error in _ACCESS_ERRORS:
        app.add_exception_handler(error, _access_error)
