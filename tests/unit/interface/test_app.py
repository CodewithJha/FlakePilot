import asyncio
import importlib.metadata
import json
import socket
import subprocess
import sys
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from support import (
    FAKE_DSN,
    FAKE_PASSWORD,
    SETTINGS,
    FakeEngine,
    FakeEngineFactory,
    Response,
    get,
    is_uuid,
    request,
)

from flakepilot.identity.application.authorization import (
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.interface.composition_root import create_app
from flakepilot.interface.http.errors import Unauthenticated
from flakepilot.interface.http.request_context import (
    AUTH_NOT_CONFIGURED,
    FORBIDDEN,
    HTTP_ERROR,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    UNAUTHENTICATED,
    VALIDATION_ERROR,
)


def logs(stderr: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stderr.splitlines()]


def request_log(stderr: str) -> dict[str, Any]:
    return next(entry for entry in logs(stderr) if entry["logger"] == "flakepilot.http")


def test_import_has_no_side_effects() -> None:
    code = (
        "import logging, sys, fastapi\n"
        "import flakepilot.interface.composition_root as root\n"
        "assert not logging.getLogger().handlers\n"
        "assert not any(isinstance(v, fastapi.FastAPI) for v in vars(root).values())\n"
        "assert 'uvicorn' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_creating_the_app_does_no_database_work() -> None:
    factory = FakeEngineFactory()
    app = create_app(SETTINGS, factory)
    assert get(app, "/health/ready")[0] == 503
    assert factory.dsns == []
    assert factory.engine.connects == 0


def test_lifespan_wires_the_null_assist_adapter() -> None:
    from flakepilot.integrations.infrastructure.ai import NullAiAssist

    app = create_app(SETTINGS, FakeEngineFactory())

    async def scenario() -> type[object]:
        async with app.router.lifespan_context(app):
            return type(app.state.assist._port)

    assert asyncio.run(scenario()) is NullAiAssist


def test_only_expected_routes_are_registered() -> None:
    app = create_app(SETTINGS)
    assert set(app.openapi()["paths"]) == {
        "/health/live",
        "/health/ready",
        "/auth/github/login",
        "/auth/github/callback",
        "/auth/logout",
        "/ingest/junit",
        "/orgs/{org_id}/runs/{run_id}",
        "/orgs/{org_id}/runs/{run_id}/classification",
        "/orgs/{org_id}/repos/{repo_id}/runs",
        "/orgs/{org_id}/runs/{run_id}/investigation",
        "/orgs/{org_id}/runs/{run_id}/investigation/brief",
        "/orgs/{org_id}/reproductions",
        "/orgs/{org_id}/reproductions/{execution_id}",
        "/orgs/{org_id}/reproductions/{execution_id}/artifacts/{kind}",
    }
    assert [getattr(r, "path", None) for r in app.routes if hasattr(r, "path")] == [
        "/openapi.json"
    ]
    assert get(app, "/openapi.json")[0] == 200
    assert get(app, "/docs")[0] == 404
    assert get(app, "/redoc")[0] == 404


INVESTIGATION = (
    "/orgs/11111111-2222-4333-8444-555555555555"
    "/runs/cccccccc-dddd-4eee-8fff-000000000001/investigation"
)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/github/login"),
        ("GET", "/auth/github/callback?code=c&state=s"),
        ("POST", "/auth/logout"),
        ("POST", INVESTIGATION),
        ("POST", f"{INVESTIGATION}/brief"),
    ],
)
def test_auth_endpoints_fail_closed_without_sign_in_configuration(
    method: str, path: str
) -> None:
    app = create_app(SETTINGS)
    status, headers, body = asyncio.run(request(app, method, path))
    assert (status, body["error"]["code"]) == (503, AUTH_NOT_CONFIGURED)
    assert "set-cookie" not in headers
    assert get(app, "/health/live")[0] == 200


def test_openapi_version_is_the_package_version() -> None:
    app = create_app(SETTINGS)
    _, _, schema = get(app, "/openapi.json")
    assert schema["info"]["version"] == importlib.metadata.version("flakepilot")


def test_startup_connects_and_readiness_follows_lifespan_and_database() -> None:
    factory = FakeEngineFactory()
    engine = factory.engine
    app = create_app(SETTINGS, factory)

    async def scenario() -> dict[str, Response]:
        seen = {"before": await request(app, "GET", "/health/ready")}
        async with app.router.lifespan_context(app):
            assert factory.dsns == [FAKE_DSN]
            assert engine.connects == 1  # the startup check
            seen["ready"] = await request(app, "GET", "/health/ready")
            engine.up = False
            seen["db_down"] = await request(app, "GET", "/health/ready")
            seen["live_db_down"] = await request(app, "GET", "/health/live")
            engine.up = True
            seen["recovered"] = await request(app, "GET", "/health/ready")
        connects_at_shutdown = engine.connects
        seen["after"] = await request(app, "GET", "/health/ready")
        assert engine.connects == connects_at_shutdown  # no probing once shut down
        return seen

    seen = asyncio.run(scenario())
    not_ready = (503, {"status": "not_ready"})
    assert (seen["before"][0], seen["before"][2]) == not_ready
    assert (seen["ready"][0], seen["ready"][2]) == (200, {"status": "ready"})
    assert (seen["db_down"][0], seen["db_down"][2]) == not_ready
    assert (seen["live_db_down"][0], seen["live_db_down"][2]) == (200, {"status": "ok"})
    assert (seen["recovered"][0], seen["recovered"][2]) == (200, {"status": "ready"})
    assert (seen["after"][0], seen["after"][2]) == not_ready
    assert engine.disposed


def test_failed_readiness_check_is_logged_without_connection_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = FakeEngineFactory()
    app = create_app(SETTINGS, factory)

    async def scenario() -> Response:
        async with app.router.lifespan_context(app):
            factory.engine.up = False
            return await request(app, "GET", "/health/ready")

    status, headers, body = asyncio.run(scenario())
    assert status == 503
    err = capsys.readouterr().err
    check = next(e for e in logs(err) if e["logger"] == "flakepilot.database")
    assert check["message"] == "database check failed: ConnectionRefusedError"
    assert check["request_id"] == headers["x-request-id"]
    assert FAKE_PASSWORD not in err + json.dumps(body)


@pytest.mark.parametrize("fails_at", ["factory", "check"])
def test_startup_failure_disposes_the_engine_and_hides_driver_details(
    fails_at: str, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = FakeEngine()
    engine.up = False
    factory = FakeEngineFactory(engine)

    def failing_factory(dsn: str) -> AsyncEngine:
        raise ValueError(f"could not parse {dsn}")

    app = create_app(SETTINGS, failing_factory if fails_at == "factory" else factory)

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(start())
    assert str(excinfo.value) == "database unavailable at startup"
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert engine.disposed == (fails_at == "check")
    assert not app.state.ready
    err = capsys.readouterr().err
    assert "database unavailable at startup" in err
    assert FAKE_PASSWORD not in err


def test_database_errors_in_requests_are_generic_and_logged_by_class_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(SETTINGS)

    @app.get("/test-only/db-error")
    async def db_error() -> None:
        raise DBAPIError(
            "SELECT secret_column FROM t WHERE pw = :pw",
            {"pw": FAKE_PASSWORD},
            Exception(f"server said no to {FAKE_DSN}"),
        )

    status, headers, body = get(app, "/test-only/db-error")
    assert status == 500
    assert body == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": headers["x-request-id"],
        }
    }
    err = capsys.readouterr().err
    entry = next(e for e in logs(err) if e["logger"] == "flakepilot.database")
    assert entry["message"] == "database error: DBAPIError"
    assert entry["request_id"] == headers["x-request-id"]
    assert "exception" not in entry
    for leak in ("secret_column", FAKE_PASSWORD, "db.invalid"):
        assert leak not in err + json.dumps(body)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(61, "Connect call failed ('10.9.8.7', 6543)"),
        socket.gaierror(8, "nodename nor servname provided: db-host-77.example"),
        TimeoutError("connect to 10.9.8.7:6543 timed out"),
    ],
)
def test_connection_errors_in_requests_are_generic_and_logged_by_class_only(
    error: OSError, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(SETTINGS)

    @app.get("/test-only/connection-error")
    async def connection_error() -> None:
        raise error

    status, headers, body = get(app, "/test-only/connection-error")
    assert status == 500
    assert body["error"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "request_id": headers["x-request-id"],
    }
    err = capsys.readouterr().err
    entry = next(e for e in logs(err) if e["logger"] == "flakepilot.database")
    assert entry["message"] == f"connection error: {type(error).__name__}"
    assert entry["request_id"] == headers["x-request-id"]
    assert "exception" not in entry
    for leak in ("10.9.8.7", "6543", "db-host-77"):
        assert leak not in err + json.dumps(body)


def test_valid_inbound_request_id_is_logged_as_client_request_id_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(SETTINGS)
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
    app = create_app(SETTINGS)
    _, headers, _ = get(app, "/health/live", inbound)
    assert is_uuid(headers["x-request-id"])

    log = request_log(capsys.readouterr().err)
    assert log["request_id"] == headers["x-request-id"]
    assert "client_request_id" not in log


def test_request_log_uses_route_templates_not_path_values_or_queries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(SETTINGS)

    @app.get("/test-only/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"ok": "yes"}

    assert get(app, "/test-only/items/SENTINEL-path-55?token=abc123")[0] == 200
    assert get(app, "/health/live")[0] == 200
    err = capsys.readouterr().err
    messages = [e["message"] for e in logs(err) if e["logger"] == "flakepilot.http"]
    assert messages == ["GET /test-only/items/{item_id} 200", "GET /health/live 200"]
    assert "SENTINEL-path-55" not in err and "abc123" not in err


def test_request_log_truncates_long_unmatched_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(SETTINGS)
    path = "/" + "x" * 300
    assert get(app, path)[0] == 404
    log = request_log(capsys.readouterr().err)
    assert log["message"] == f"GET {path[:256]}… 404"


def test_unknown_route_and_wrong_method_use_the_error_shape() -> None:
    app = create_app(SETTINGS)

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


def test_every_error_code_has_its_status_and_request_id() -> None:
    app = create_app(SETTINGS)

    @app.get("/test-only/int/{value}")
    async def typed(value: int) -> dict[str, int]:
        return {"value": value}

    @app.get("/test-only/teapot")
    async def teapot() -> None:
        raise HTTPException(418)

    @app.get("/test-only/crash")
    async def crash() -> None:
        raise RuntimeError("boom")

    cases = [
        ("GET", "/nope", 404, NOT_FOUND),
        ("POST", "/health/live", 405, METHOD_NOT_ALLOWED),
        ("GET", "/test-only/int/x", 422, VALIDATION_ERROR),
        ("GET", "/test-only/teapot", 418, HTTP_ERROR),
        ("GET", "/test-only/crash", 500, INTERNAL_ERROR),
    ]
    for method, path, status, code in cases:
        got, headers, body = asyncio.run(request(app, method, path))
        assert (got, body["error"]["code"]) == (status, code), path
        assert body["error"]["request_id"] == headers["x-request-id"], path
        assert is_uuid(headers["x-request-id"])


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (Unauthenticated, 401, UNAUTHENTICATED),
        (PermissionDenied, 403, FORBIDDEN),
        (ResourceNotFound, 404, NOT_FOUND),
    ],
)
def test_access_errors_map_to_stable_codes_without_leaking_ids(
    error: type[Exception], status: int, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(SETTINGS)

    @app.get("/test-only/protected")
    async def protected() -> None:
        raise error("org-SENTINEL-41 principal-SENTINEL-42")

    got, headers, body = get(app, "/test-only/protected")
    assert (got, body["error"]["code"]) == (status, code)
    assert body["error"]["request_id"] == headers["x-request-id"]
    if error is ResourceNotFound:  # indistinguishable from a route that does not exist
        _, _, unknown = get(app, "/no-such-route")
        assert {**body["error"], "request_id": None} == {
            **unknown["error"],
            "request_id": None,
        }
    assert "SENTINEL" not in json.dumps(body) + capsys.readouterr().err


SENTINEL = "SENTINEL-zq81"


@pytest.mark.parametrize(
    ("path", "body", "fields"),
    [
        (f"/test-only/items/{SENTINEL}", None, [(["path", "item_id"], "int_parsing")]),
        (
            f"/test-only/items/1?limit={SENTINEL}",
            None,
            [(["query", "limit"], "int_parsing")],
        ),
        (
            "/test-only/items/1",
            f'{{"name": 5, "{SENTINEL}": 1}}',
            [(["body", "name"], "string_type"), (["body"], "extra_forbidden")],
        ),
        (
            "/test-only/items/1",
            f'{{"name": {SENTINEL}',
            [(["body", 9], "json_invalid")],
        ),
    ],
)
def test_validation_errors_never_echo_client_input(
    path: str, body: str | None, fields: list[tuple[list[object], str]]
) -> None:
    app = create_app(SETTINGS)

    class Item(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str

    @app.post("/test-only/items/{item_id}")
    async def create(item_id: int, item: Item | None = None, limit: int = 1) -> None:
        pass

    status, headers, response = asyncio.run(
        request(
            app,
            "POST",
            path,
            [("content-type", "application/json")],
            body=(body or "").encode(),
        )
    )
    assert status == 422
    assert response["error"]["code"] == VALIDATION_ERROR
    assert response["error"]["request_id"] == headers["x-request-id"]
    assert [(f["loc"], f["type"]) for f in response["error"]["fields"]] == fields
    assert SENTINEL not in json.dumps(response)


def test_validation_error_reports_fields_without_echoing_input() -> None:
    app = create_app(SETTINGS)

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
    app = create_app(SETTINGS)

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

    entries = logs(capsys.readouterr().err)
    error = next(e for e in entries if e["message"] == "unhandled exception")
    assert error["request_id"] == request_id
    assert error["level"] == "ERROR"
    assert "sensitive internal detail" in error["exception"]
    access = next(
        e for e in entries if e["logger"] == "flakepilot.http" and e is not error
    )
    assert access["message"] == "GET /test-only/boom 500"
    assert access["request_id"] == request_id
    assert set(access) == {"timestamp", "level", "logger", "message", "request_id"}
    assert "abc123" not in json.dumps(entries)
