import asyncio
import importlib.metadata
import json
import subprocess
import sys
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.types import Message

from flakepilot.interface.composition_root import Settings, create_app

Response = tuple[int, dict[str, str], Any]


async def request(
    app: FastAPI, method: str, target: str, headers: list[tuple[str, str]] | None = None
) -> Response:
    """Drive the ASGI app directly (httpx, needed by TestClient, pulls in MPL-2.0 certifi)."""
    path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers or []],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], response_headers, json.loads(body)


def get(
    app: FastAPI, target: str, headers: list[tuple[str, str]] | None = None
) -> Response:
    return asyncio.run(request(app, "GET", target, headers))


def is_uuid(value: str) -> bool:
    return str(uuid.UUID(value)) == value


def test_import_has_no_side_effects() -> None:
    code = (
        "import logging, sys, fastapi\n"
        "import flakepilot.interface.composition_root as root\n"
        "assert not logging.getLogger().handlers\n"
        "assert not any(isinstance(v, fastapi.FastAPI) for v in vars(root).values())\n"
        "assert 'uvicorn' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_only_health_and_openapi_routes_are_registered() -> None:
    app = create_app(Settings())
    assert set(app.openapi()["paths"]) == {"/health/live", "/health/ready"}
    assert [getattr(r, "path", None) for r in app.routes if hasattr(r, "path")] == [
        "/openapi.json"
    ]
    assert get(app, "/openapi.json")[0] == 200
    assert get(app, "/docs")[0] == 404
    assert get(app, "/redoc")[0] == 404


def test_liveness_and_readiness_follow_the_lifespan() -> None:
    app = create_app(Settings())

    async def scenario() -> list[Response]:
        before = await request(app, "GET", "/health/ready")
        async with app.router.lifespan_context(app):
            during = [
                await request(app, "GET", "/health/ready"),
                await request(app, "GET", "/health/live"),
            ]
        after = await request(app, "GET", "/health/ready")
        return [before, *during, after]

    before, ready, live, after = asyncio.run(scenario())
    assert (before[0], before[2]) == (503, {"status": "not_ready"})
    assert (ready[0], ready[2]) == (200, {"status": "ready"})
    assert (live[0], live[2]) == (200, {"status": "ok"})
    assert (after[0], after[2]) == (503, {"status": "not_ready"})
    assert get(app, "/health/live")[0] == 200


def request_log(stderr: str) -> dict[str, Any]:
    return next(
        entry
        for entry in map(json.loads, stderr.splitlines())
        if entry["logger"] == "flakepilot.http"
    )


def test_openapi_version_is_the_package_version() -> None:
    app = create_app(Settings())
    _, _, schema = get(app, "/openapi.json")
    assert schema["info"]["version"] == importlib.metadata.version("flakepilot")


def test_valid_inbound_request_id_is_logged_as_client_request_id_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(Settings())
    status, headers, _ = get(app, "/health/live", [("X-Request-ID", "ci-run_42.a")])
    assert status == 200
    assert is_uuid(headers["x-request-id"])

    log = request_log(capsys.readouterr().err)
    assert log["request_id"] == headers["x-request-id"]
    assert log["client_request_id"] == "ci-run_42.a"


@pytest.mark.parametrize(
    "inbound",
    [
        [],
        [("X-Request-ID", "has space")],
        [("X-Request-ID", "x" * 65)],
        [("X-Request-ID", "")],
        [("X-Request-ID", "a"), ("X-Request-ID", "b")],
    ],
)
def test_missing_or_invalid_inbound_request_id_is_ignored(
    inbound: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(Settings())
    _, headers, _ = get(app, "/health/live", inbound)
    assert is_uuid(headers["x-request-id"])

    log = request_log(capsys.readouterr().err)
    assert log["request_id"] == headers["x-request-id"]
    assert "client_request_id" not in log


def test_unknown_route_and_wrong_method_use_the_error_shape() -> None:
    app = create_app(Settings())

    status, headers, body = get(app, "/nope")
    assert status == 404
    assert body == {
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "request_id": headers["x-request-id"],
        }
    }

    status, headers, body = asyncio.run(request(app, "POST", "/health/live"))
    assert status == 405
    assert headers["allow"] == "GET"
    assert body["error"]["code"] == "method_not_allowed"
    assert body["error"]["request_id"] == headers["x-request-id"]


def test_validation_error_reports_fields_without_echoing_input() -> None:
    app = create_app(Settings())

    @app.get("/test-only/items")
    async def items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    status, headers, body = get(app, "/test-only/items?limit=hunter2-secret")
    assert status == 422
    assert body == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed",
            "request_id": headers["x-request-id"],
            "fields": [{"loc": ["query", "limit"], "type": "int_parsing"}],
        }
    }
    assert "hunter2" not in json.dumps(body)


def test_unhandled_exception_is_generic_for_clients_and_logged_for_operators(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(Settings())

    @app.get("/test-only/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal detail")

    status, headers, body = get(app, "/test-only/boom?token=abc123")
    request_id = headers["x-request-id"]
    assert status == 500
    assert body == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": request_id,
        }
    }

    logs = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    error = next(entry for entry in logs if entry["message"] == "unhandled exception")
    assert error["request_id"] == request_id
    assert error["level"] == "ERROR"
    assert "sensitive internal detail" in error["exception"]
    access = next(
        entry
        for entry in logs
        if entry["logger"] == "flakepilot.http" and entry is not error
    )
    assert access["message"] == "GET /test-only/boom 500"
    assert access["request_id"] == request_id
    assert set(access) == {"timestamp", "level", "logger", "message", "request_id"}
    assert "abc123" not in json.dumps(logs)
