import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from support import FAKE_DSN, FAKE_PASSWORD, FakeEngine

from flakepilot.interface.composition_root import database
from flakepilot.interface.composition_root.database import (
    POOL_SIZE,
    POOL_TIMEOUT,
    ReadinessCheck,
    create_engine,
    dispose_engine,
)

ROOT = Path(__file__).resolve().parents[3]


def test_readiness_check_gives_up_on_a_hanging_database() -> None:
    engine = FakeEngine()
    engine.hang = True
    started = time.monotonic()
    assert asyncio.run(ReadinessCheck(engine.as_engine(), timeout=0.1)()) is False
    assert time.monotonic() - started < 1.0


def test_concurrent_readiness_checks_share_one_probe() -> None:
    engine = FakeEngine()
    engine.delay = 0.05
    check = ReadinessCheck(engine.as_engine())

    async def scenario() -> list[bool]:
        results = await asyncio.gather(*(check() for _ in range(20)))
        results.append(await check())
        return results

    assert asyncio.run(scenario()) == [True] * 21
    assert engine.connects == 2  # one shared probe, then a fresh one: no caching


def test_dispose_abandons_connections_when_graceful_dispose_hangs() -> None:
    engine = FakeEngine()
    engine.hang_on_dispose = True
    started = time.monotonic()
    asyncio.run(dispose_engine(engine.as_engine(), timeout=0.1))
    assert engine.abandoned and not engine.disposed
    assert time.monotonic() - started < 1.0


def test_engine_is_configured_without_connecting() -> None:
    engine = create_engine(FAKE_DSN)  # db.invalid: any connection attempt would fail
    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.pool.size() == POOL_SIZE  # type: ignore[attr-defined]
    assert engine.pool.timeout() == POOL_TIMEOUT  # type: ignore[attr-defined]
    assert engine.sync_engine.hide_parameters
    assert engine.pool._pre_ping
    assert FAKE_PASSWORD not in repr(engine) + str(engine.url)


def test_offline_migration_sql_needs_no_database_url() -> None:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "BEGIN;" in result.stdout and "COMMIT;" in result.stdout


def _captured(monkeypatch: pytest.MonkeyPatch, dsn: str) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def fake(url: Any, **arguments: Any) -> str:
        seen["url"], seen["arguments"] = url, arguments
        return "engine"

    monkeypatch.setattr(database, "create_async_engine", fake)
    create_engine(dsn)
    return seen["url"], seen["arguments"]["connect_args"]


@pytest.mark.parametrize("mode", ["require", "verify-full", "disable"])
def test_a_libpq_sslmode_becomes_the_asyncpg_ssl_argument(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    url, connect_args = _captured(
        monkeypatch, f"{FAKE_DSN}?sslmode={mode}&application_name=fp"
    )
    assert "sslmode" not in url.query
    assert url.query["application_name"] == "fp"
    assert url.drivername == "postgresql+asyncpg"
    assert connect_args["ssl"] == mode
    assert connect_args["timeout"] == database.CONNECT_TIMEOUT


def test_without_sslmode_no_ssl_argument_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, connect_args = _captured(monkeypatch, FAKE_DSN)
    assert "ssl" not in connect_args


def test_an_unknown_sslmode_is_refused_without_echoing_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError) as refused:
        _captured(monkeypatch, f"{FAKE_DSN}?sslmode=sometimes")
    assert FAKE_PASSWORD not in str(refused.value)
    assert "sometimes" not in str(refused.value)
