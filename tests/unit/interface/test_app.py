import asyncio
import importlib.metadata
import json
import subprocess
import sys
from typing import Any

import pytest
from support import (
    FAKE_DSN,
    FAKE_PASSWORD,
    SETTINGS,
    FakePool,
    FakePoolFactory,
    Response,
    get,
    is_uuid,
    request,
)

from flakepilot.interface.composition_root import create_app


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
    factory = FakePoolFactory()
    app = create_app(SETTINGS, factory)
    assert get(app, "/health/ready")[0] == 503
    assert factory.dsns == []
    assert factory.pool.acquires == 0


def test_only_health_and_openapi_routes_are_registered() -> None:
    app = create_app(SETTINGS)
    assert set(app.openapi()["paths"]) == {"/health/live", "/health/ready"}
    assert [getattr(r, "path", None) for r in app.routes if hasattr(r, "path")] == [
        "/openapi.json"
    ]
    assert get(app, "/openapi.json")[0] == 200
    assert get(app, "/docs")[0] == 404
    assert get(app, "/redoc")[0] == 404


def test_openapi_version_is_the_package_version() -> None:
    app = create_app(SETTINGS)
    _, _, schema = get(app, "/openapi.json")
    assert schema["info"]["version"] == importlib.metadata.version("flakepilot")


def test_startup_connects_and_readiness_follows_lifespan_and_database() -> None:
    factory = FakePoolFactory()
    pool = factory.pool
    app = create_app(SETTINGS, factory)

    async def scenario() -> dict[str, Response]:
        seen = {"before": await request(app, "GET", "/health/ready")}
        async with app.router.lifespan_context(app):
            assert factory.dsns == [FAKE_DSN]
            assert pool.acquires == 1  # the startup check
            seen["ready"] = await request(app, "GET", "/health/ready")
            pool.up = False
            seen["db_down"] = await request(app, "GET", "/health/ready")
            seen["live_db_down"] = await request(app, "GET", "/health/live")
            pool.up = True
            seen["recovered"] = await request(app, "GET", "/health/ready")
        acquires_at_shutdown = pool.acquires
        seen["after"] = await request(app, "GET", "/health/ready")
        assert pool.acquires == acquires_at_shutdown  # no probing once shut down
        return seen

    seen = asyncio.run(scenario())
    not_ready = (503, {"status": "not_ready"})
    assert (seen["before"][0], seen["before"][2]) == not_ready
    assert (seen["ready"][0], seen["ready"][2]) == (200, {"status": "ready"})
    assert (seen["db_down"][0], seen["db_down"][2]) == not_ready
    assert (seen["live_db_down"][0], seen["live_db_down"][2]) == (200, {"status": "ok"})
    assert (seen["recovered"][0], seen["recovered"][2]) == (200, {"status": "ready"})
    assert (seen["after"][0], seen["after"][2]) == not_ready
    assert pool.closed


def test_failed_readiness_check_is_logged_without_connection_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = FakePoolFactory()
    app = create_app(SETTINGS, factory)

    async def scenario() -> Response:
        async with app.router.lifespan_context(app):
            factory.pool.up = False
            return await request(app, "GET", "/health/ready")

    status, headers, body = asyncio.run(scenario())
    assert status == 503
    err = capsys.readouterr().err
    check = next(e for e in logs(err) if e["logger"] == "flakepilot.database")
    assert check["message"] == "database check failed: ConnectionRefusedError"
    assert check["request_id"] == headers["x-request-id"]
    assert FAKE_PASSWORD not in err + json.dumps(body)


@pytest.mark.parametrize("fails_at", ["factory", "check"])
def test_startup_failure_closes_the_pool_and_hides_driver_details(
    fails_at: str, capsys: pytest.CaptureFixture[str]
) -> None:
    pool = FakePool()
    pool.up = False

    async def failing_factory(dsn: str) -> FakePool:
        raise OSError(f"cannot reach {dsn}")

    factory = failing_factory if fails_at == "factory" else FakePoolFactory(pool)
    app = create_app(SETTINGS, factory)

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(start())
    assert str(excinfo.value) == "database unavailable at startup"
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert pool.closed == (fails_at == "check")
    assert not app.state.ready
    err = capsys.readouterr().err
    assert "database unavailable at startup" in err
    assert FAKE_PASSWORD not in err


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
