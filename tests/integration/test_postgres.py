"""Real-PostgreSQL tests. Each test gets a throwaway database that is dropped afterwards."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from support import admin_execute, request

from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.database import create_engine, transaction

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _leaks(url: str) -> list[str]:
    parsed = make_url(url)
    return [url, *(p for p in (parsed.password, parsed.database) if p)]


def test_startup_readiness_and_shutdown(database_url: str) -> None:
    app = create_app(Settings(Secret(database_url)))

    async def scenario() -> bool:
        async with app.router.lifespan_context(app):
            ready: bool = await app.state.readiness_probe()
        return ready

    assert asyncio.run(scenario()) is True
    assert app.state.ready is False


def test_commit_is_visible_and_failures_roll_back(database_url: str) -> None:
    async def scenario() -> list[int]:
        engine = create_engine(database_url)
        try:
            async with transaction(engine) as session:
                await session.execute(text("CREATE TABLE items (id int PRIMARY KEY)"))
            async with transaction(engine) as session:
                await session.execute(text("INSERT INTO items VALUES (1)"))
            with pytest.raises(RuntimeError):
                async with transaction(engine) as session:
                    await session.execute(text("INSERT INTO items VALUES (2)"))
                    raise RuntimeError("use case failed")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2), transaction(engine) as session:
                    await session.execute(text("INSERT INTO items VALUES (3)"))
                    await asyncio.sleep(5)
            async with transaction(engine) as session:
                rows = await session.execute(text("SELECT id FROM items ORDER BY id"))
                result: list[int] = list(rows.scalars())
        finally:
            # The suite may run against one persistent database (shared mode),
            # and this table lives outside the migration schemas; clean it up.
            try:
                async with transaction(engine) as session:
                    await session.execute(text("DROP TABLE IF EXISTS items"))
            finally:
                await engine.dispose()
        return result

    assert asyncio.run(scenario()) == [1]


def test_statement_timeout_applies_fires_and_leaves_the_connection_usable(
    database_url: str,
) -> None:
    async def scenario() -> tuple[list[str], float, str, int, int]:
        engine = create_engine(database_url, pool_size=1)  # one connection, reused
        try:
            async with transaction(engine) as session:
                configured = [
                    (await session.execute(text(f"SHOW {name}"))).scalar_one()
                    for name in (
                        "statement_timeout",
                        "idle_in_transaction_session_timeout",
                    )
                ]
            started = time.monotonic()
            with pytest.raises(exc.DBAPIError) as raised:
                async with transaction(engine) as session:
                    await session.execute(text("SET LOCAL statement_timeout = '100ms'"))
                    await session.execute(text("SELECT pg_sleep(5)"))
            elapsed = time.monotonic() - started
            sqlstate = getattr(raised.value.orig, "sqlstate", "")
            async with transaction(engine) as session:
                after = (await session.execute(text("SELECT 1"))).scalar_one()
            return configured, elapsed, sqlstate, after, engine.pool.checkedout()  # type: ignore[attr-defined]
        finally:
            await engine.dispose()

    configured, elapsed, sqlstate, after, checked_out = asyncio.run(scenario())
    assert configured == ["30s", "1min"]
    assert sqlstate == "57014"  # query_canceled
    assert elapsed < 2.0
    assert after == 1
    assert checked_out == 0


def test_pool_acquisition_times_out_when_the_pool_is_exhausted(
    database_url: str,
) -> None:
    async def scenario() -> float:
        engine = create_engine(database_url, pool_size=1, pool_timeout=0.3)
        try:
            async with engine.connect() as held:
                await held.execute(text("SELECT 1"))
                started = time.monotonic()
                with pytest.raises(exc.TimeoutError):
                    async with engine.connect():
                        pass
                return time.monotonic() - started
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) < 2.0


def test_readiness_stays_bounded_when_the_pool_would_wait_longer(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    engines: list[AsyncEngine] = []

    def factory(dsn: str) -> AsyncEngine:
        engines.append(create_engine(dsn, pool_size=1, pool_timeout=10))
        return engines[-1]

    app = create_app(Settings(Secret(database_url)), factory)

    async def scenario() -> tuple[bool, float, bool]:
        async with app.router.lifespan_context(app):
            async with engines[0].connect() as held:
                await held.execute(text("SELECT 1"))
                started = time.monotonic()
                exhausted = await app.state.readiness_probe()
                elapsed = time.monotonic() - started
            recovered = await app.state.readiness_probe()
        return exhausted, elapsed, recovered

    exhausted, elapsed, recovered = asyncio.run(scenario())
    assert exhausted is False
    assert 1.5 < elapsed < 3.5  # the 2s readiness bound, not the 10s pool timeout
    assert recovered is True
    err = capsys.readouterr().err
    assert "database check failed: TimeoutError" in err
    assert not any(leak in err for leak in _leaks(database_url))


def test_database_errors_in_requests_hide_sql_and_credentials(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    engines: list[AsyncEngine] = []

    def factory(dsn: str) -> AsyncEngine:
        engines.append(create_engine(dsn))
        return engines[-1]

    app = create_app(Settings(Secret(database_url)), factory)

    @app.get("/test-only/slow")
    async def slow() -> None:
        async with transaction(engines[0]) as session:
            await session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            await session.execute(text("SELECT pg_sleep(5) AS marker_sql"))

    async def scenario() -> tuple[int, object]:
        async with app.router.lifespan_context(app):
            status, _, body = await request(app, "GET", "/test-only/slow")
        return status, body

    status, body = asyncio.run(scenario())
    assert status == 500
    assert isinstance(body, dict) and body["error"]["code"] == "internal_error"
    output = capsys.readouterr().err + json.dumps(body)
    assert "database error: " in output
    assert "marker_sql" not in output and "pg_sleep" not in output
    assert not any(leak in output for leak in _leaks(database_url))


def test_unreachable_database_during_a_request_is_generic(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as probe:  # a port with nothing listening
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    unreachable = create_engine(
        f"postgresql://flakepilot:unreach-pw@127.0.0.1:{port}/x"
    )
    app = create_app(Settings(Secret(database_url)))

    @app.get("/test-only/unreachable")
    async def unreachable_route() -> None:
        async with transaction(unreachable) as session:
            await session.execute(text("SELECT 1"))

    async def scenario() -> tuple[int, dict[str, str], object]:
        try:
            async with app.router.lifespan_context(app):
                return await request(app, "GET", "/test-only/unreachable")
        finally:
            await unreachable.dispose()

    status, headers, body = asyncio.run(scenario())
    assert status == 500
    assert isinstance(body, dict) and body["error"]["code"] == "internal_error"
    err = capsys.readouterr().err
    entry = next(
        e
        for e in map(json.loads, err.splitlines())
        if e["logger"] == "flakepilot.database"
    )
    assert entry["message"] == "connection error: ConnectionRefusedError"
    assert entry["request_id"] == headers["x-request-id"]
    for leak in (f"'127.0.0.1', {port}", "unreach-pw", "Connect call failed"):
        assert leak not in err + json.dumps(body)


@pytest.mark.parametrize("pre_ping", [True, False])
def test_pre_ping_replaces_a_connection_the_server_dropped(
    database_url: str, pre_ping: bool
) -> None:
    async def scenario() -> tuple[int, int | None]:
        engine = create_engine(database_url, pool_size=1, pool_pre_ping=pre_ping)
        try:
            async with transaction(engine) as session:
                pid = (
                    await session.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            await admin_execute(database_url, f"SELECT pg_terminate_backend({pid})")
            await asyncio.sleep(0.2)
            try:
                async with transaction(engine) as session:
                    query = text("SELECT pg_backend_pid()")
                    return pid, (await session.execute(query)).scalar_one()
            except (exc.DBAPIError, OSError):
                return pid, None
        finally:
            await engine.dispose()

    first, second = asyncio.run(scenario())
    if pre_ping:
        assert second is not None and second != first  # transparently reconnected
    else:
        assert second is None  # without pre-ping the dead connection fails the request


def test_migrations_upgrade_and_check_cleanly(database_url: str) -> None:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    environ["FLAKEPILOT_DATABASE_URL"] = database_url

    def alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=environ,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    async def schemas() -> list[object]:
        engine = create_engine(database_url)
        try:
            async with transaction(engine) as session:
                found = await session.execute(
                    text(
                        "SELECT to_regclass('alembic_version'), "
                        "to_regclass('iam.outbox'), to_regnamespace('int_secrets'), "
                        "to_regclass('ti.uploads')"
                    )
                )
                return list(found.one())
        finally:
            await engine.dispose()

    # Up, check, down to nothing, up again, check again.
    runs = [
        alembic("upgrade", "head"),
        alembic("check"),
        alembic("downgrade", "base"),
    ]
    emptied = asyncio.run(schemas())
    runs += [alembic("upgrade", "head"), alembic("check")]
    for result in runs:
        assert result.returncode == 0, result.stderr
    for check in (runs[1], runs[4]):
        assert "No new upgrade operations detected." in check.stdout + check.stderr
    output = "".join(r.stdout + r.stderr for r in runs)
    assert not any(leak in output for leak in _leaks(database_url))
    assert emptied == ["alembic_version", None, None, None]
    assert asyncio.run(schemas()) == [
        "alembic_version",
        "iam.outbox",
        "int_secrets",
        "ti.uploads",
    ]


def test_the_configured_ai_endpoint_wires_the_advisory_port(database_url: str) -> None:
    """ADR-017: an operator-configured local endpoint enables advisory assist."""
    app = create_app(
        Settings(
            Secret(database_url),
            ai_endpoint="http://127.0.0.1:11434/v1/chat/completions",
            ai_model="gemma_3_4b_it",
        )
    )

    async def scenario() -> str:
        async with app.router.lifespan_context(app):
            provider = app.state.assist._port.describe().provider
        return str(provider)

    assert asyncio.run(scenario()) == "local_endpoint"
    assert app.state.ready is False


def test_without_the_ai_endpoint_assist_stays_null(database_url: str) -> None:
    app = create_app(Settings(Secret(database_url)))

    async def scenario() -> str:
        async with app.router.lifespan_context(app):
            provider = app.state.assist._port.describe().provider
        return str(provider)

    assert asyncio.run(scenario()) == "none"
