from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from flakepilot.interface.http.request_context import error_body, request_id_var


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    status = HTTPStatus(exc.status_code)
    code = status.phrase.lower().replace(" ", "_").replace("-", "_")
    message = exc.detail if isinstance(exc.detail, str) else status.phrase
    return JSONResponse(
        error_body(code, message, request_id_var.get()),
        exc.status_code,
        headers=exc.headers,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 with field locations and error types only.

    Pydantic's `input`, `ctx` and `msg` can echo client-supplied values, so they are dropped.
    """
    assert isinstance(exc, RequestValidationError)
    body = error_body(
        "validation_error", "Request validation failed", request_id_var.get()
    )
    body["error"]["fields"] = [
        {"loc": e["loc"], "type": e["type"]} for e in exc.errors()
    ]
    return JSONResponse(body, 422)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
