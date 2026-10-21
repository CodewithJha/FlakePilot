"""Maps framework errors to the stable error contract (see `request_context`).

Future expected failures from use cases: application layers raise plain exception classes
defined in their own context (no FastAPI, no HTTP), and this module maps each class to a
status and a new stable code when the first such exception exists. Nothing raises one yet.
"""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from flakepilot.interface.http.request_context import (
    HTTP_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    VALIDATION_ERROR,
    error_body,
    request_id_var,
)

_HTTP_CODES = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}


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
