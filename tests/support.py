"""Test helpers: a direct ASGI caller, an in-memory stand-in for the database engine, and
throwaway PostgreSQL databases for integration tests."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError
from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine

FAKE_PASSWORD = "Zx9-fake-pw-7Qk"
FAKE_DSN = f"postgresql://flakepilot:{FAKE_PASSWORD}@db.invalid:5432/flakepilot"
SETTINGS = Settings(Secret(FAKE_DSN))

Response = tuple[int, dict[str, str], Any]


RawResponse = tuple[int, list[tuple[str, str]], bytes]


async def request(
    app: FastAPI,
    method: str,
    target: str,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> Response:
    status, raw_headers, content = await send_raw(app, method, target, headers, body)
    return status, dict(raw_headers), json.loads(content)


async def send_raw(
    app: FastAPI,
    method: str,
    target: str,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> RawResponse:
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
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    content = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    raw_headers = [(k.decode(), v.decode()) for k, v in start["headers"]]
    return start["status"], raw_headers, content


def get(
    app: FastAPI, target: str, headers: list[tuple[str, str]] | None = None
) -> Response:
    return asyncio.run(request(app, "GET", target, headers))


def is_uuid(value: str) -> bool:
    return str(uuid.UUID(value)) == value


async def admin_execute(url: str, statement: str) -> None:
    """Run one statement outside a transaction (e.g. CREATE DATABASE)."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@contextmanager
def throwaway_database(migrate: bool = False) -> Iterator[str]:
    """A fresh database next to FLAKEPILOT_TEST_DATABASE_URL, always dropped afterwards.

    With FLAKEPILOT_TEST_SHARED_DATABASE=1, the database at
    FLAKEPILOT_TEST_DATABASE_URL itself is used instead (see `shared_database`) —
    for hosts that refuse CREATE DATABASE, such as Tiger Cloud's managed `tsdb`.
    """
    if _shared_mode_enabled():
        with shared_database(migrate) as url:
            yield url
        return
    admin_url = os.environ["FLAKEPILOT_TEST_DATABASE_URL"]
    name = f"flakepilot_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        if migrate:
            _run_alembic_upgrade(url)
        yield url
    finally:
        asyncio.run(
            admin_execute(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        )


# Every schema the migrations create (and only those). `_shared_tables` reads the
# tables that exist from the server itself, so new migration tables join this list
# without an edit here; anything else in the database (public, TimescaleDB internals,
# tsdb_admin, an owner's own tables) is never listed and never truncated.
SHARED_SCHEMAS = (
    "aud",
    "diag",
    "exec",
    "iam",
    "int_secrets",
    "ops",
    "orch",
    "repo",
    "ti",
)

SHARED_DATABASE_ENV = "FLAKEPILOT_TEST_SHARED_DATABASE"
_shared_gate_done = False  # the suite-start safety gate runs once per test session
_shared_migrated = False  # as does the shared database's `alembic upgrade head`


def _shared_mode_enabled() -> bool:
    """Never on by accident: only an explicit `=1` selects the shared database."""
    return os.environ.get(SHARED_DATABASE_ENV) == "1"


def _quote_ident(identifier: str) -> str:
    """Quote a plain identifier, refusing anything else (fail closed)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise RuntimeError(f"shared mode: refusing irregular identifier {identifier!r}")
    return f'"{identifier}"'


async def _tables(url: str) -> list[str]:
    """Qualified whitelisted table names, with every irregular name refused."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_schema = ANY(:schemas) ORDER BY 1, 2"
                ),
                {"schemas": list(SHARED_SCHEMAS)},
            )
            return [
                f"{_quote_ident(row.table_schema)}.{_quote_ident(row.table_name)}"
                for row in rows
            ]
    finally:
        await engine.dispose()


async def _gate(url: str) -> None:
    """One connection: list, empty-table counts, and the external-FK check."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_schema = ANY(:schemas) ORDER BY 1, 2"
                ),
                {"schemas": list(SHARED_SCHEMAS)},
            )
            tables = [
                f"{_quote_ident(row.table_schema)}.{_quote_ident(row.table_name)}"
                for row in rows
            ]
            empties = [
                target
                for target in tables
                if (
                    await connection.execute(text(f"SELECT count(*) FROM {target}"))
                ).scalar_one()
            ]
            outside = [
                f"{row.foreign_table} -> {row.referenced_schema}"
                for row in await connection.execute(
                    text(
                        "SELECT DISTINCT fk_ns.nspname || '.' || fk_tbl.relname AS foreign_table, "
                        "ref_ns.nspname AS referenced_schema "
                        "FROM pg_constraint con "
                        "JOIN pg_class fk_tbl ON fk_tbl.oid = con.conrelid "
                        "JOIN pg_namespace fk_ns ON fk_ns.oid = fk_tbl.relnamespace "
                        "JOIN pg_class ref_tbl ON ref_tbl.oid = con.confrelid "
                        "JOIN pg_namespace ref_ns ON ref_ns.oid = ref_tbl.relnamespace "
                        "WHERE con.contype = 'f' AND fk_ns.nspname <> ALL(:schemas) "
                        "AND ref_ns.nspname = ANY(:schemas) ORDER BY 1"
                    ),
                    {"schemas": list(SHARED_SCHEMAS)},
                )
            ]
        if empties:
            raise RuntimeError(
                "shared test database refused: these migration tables are not empty "
                f"(possibly unrelated data): {', '.join(empties)}"
            )
        if outside:
            raise RuntimeError(
                "shared test database refused: tables outside the whitelist hold "
                f"foreign keys into it: {', '.join(outside)}"
            )
    finally:
        await engine.dispose()


def _assert_shared_tables_empty(url: str) -> None:
    """The suite-start safety gate: refuse a database that still holds data.

    Truncation after a test is only ever safe when we can prove everything it
    truncates accumulated during THIS suite run. Two hard preconditions:
    every whitelisted table is empty before the suite starts, and nothing
    outside the whitelist references the whitelist (TRUNCATE CASCADE is
    deliberately never used — it would silently truncate such tables).
    """
    asyncio.run(_gate(url))


def _truncate_shared_tables(url: str) -> None:
    """One atomic TRUNCATE of all whitelisted tables — never CASCADE, never
    RESTART IDENTITY (keys are UUIDs generated client-side), never any other
    schema (public's `alembic_version`, extension and owner objects untouched)."""

    async def run() -> None:
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_type = 'BASE TABLE' "
                        "AND table_schema = ANY(:schemas)"
                    ),
                    {"schemas": list(SHARED_SCHEMAS)},
                )
                targets = [
                    f"{_quote_ident(row.table_schema)}.{_quote_ident(row.table_name)}"
                    for row in rows
                ]
                if targets:
                    await connection.execute(
                        text(f"TRUNCATE TABLE {', '.join(targets)}")
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


@contextmanager
def shared_database(migrate: bool = False) -> Iterator[str]:
    """The database at FLAKEPILOT_TEST_DATABASE_URL itself, for a host without
    CREATE DATABASE (Tiger Cloud's `tsdb`): instead of creating a fresh database
    per test, every whitelisted table is TRUNCATEd when the fixture exits, which
    restores exactly the state the throwaway-per-test fixture hands out: an
    empty database where no test can see another test's rows.

    Fail-closed: on first use the gate refuses while anything whitelisted is
    non-empty or externally referenced (`_assert_shared_tables_empty`), so an
    owner database with real data stops the suite instead of being truncated.
    """

    global _shared_gate_done, _shared_migrated
    url = os.environ["FLAKEPILOT_TEST_DATABASE_URL"]
    if not _shared_gate_done:
        _assert_shared_tables_empty(url)
        _shared_gate_done = True
    if migrate and not _shared_migrated:
        _run_alembic_upgrade(url)
        _shared_migrated = True
    try:
        yield url
    finally:
        _truncate_shared_tables(url)


def _run_alembic_upgrade(url: str) -> None:
    """`alembic upgrade head` in a subprocess against the given URL (once)."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env={**environ, "FLAKEPILOT_DATABASE_URL": url},
        capture_output=True,
        timeout=120,
        check=True,
    )


class FakeEngine:
    """The AsyncEngine surface the composition root uses: connect(), execute(), dispose().

    Failures raise with the DSN in the message, as a driver error might.
    """

    def __init__(self) -> None:
        self.up = True
        self.delay = 0.0
        self.hang = False
        self.hang_on_dispose = False
        self.connects = 0
        self.disposed = False
        self.abandoned = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["FakeEngine"]:
        self.connects += 1
        if self.hang:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)
        if not self.up:
            raise ConnectionRefusedError(f"could not connect to {FAKE_DSN}")
        yield self

    async def execute(self, statement: object) -> None:
        assert str(statement) == "SELECT 1"

    async def dispose(self, close: bool = True) -> None:
        if not close:
            self.abandoned = True
            return
        if self.hang_on_dispose:
            await asyncio.Event().wait()
        self.disposed = True

    def as_engine(self) -> AsyncEngine:
        return cast(AsyncEngine, self)


class InMemoryArtifactStore:
    """The artifact port's test fake: content-addressed, immutable, hash-checked."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        try:
            canonical = is_uuid(org_id) and is_uuid(repo_id) and owner.isalpha()
        except ValueError:
            canonical = False
        if not canonical:
            raise ArtifactStoreError("invalid_request")
        digest = hashlib.sha256(data).hexdigest()
        key = f"{owner}/{org_id}/{repo_id}/sha256/{digest}"
        self.objects.setdefault(key, data)
        return ArtifactRef(key, digest, len(data))

    async def get(self, ref: ArtifactRef) -> bytes:
        if ref.key not in self.objects:
            raise ArtifactStoreError("not_found")
        data = self.objects[ref.key]
        if hashlib.sha256(data).hexdigest() != ref.sha256 or len(data) != ref.size:
            raise ArtifactStoreError("integrity")
        return data


class FakeEngineFactory:
    def __init__(self, engine: FakeEngine | None = None) -> None:
        self.engine = engine or FakeEngine()
        self.dsns: list[str] = []

    def __call__(self, dsn: str) -> AsyncEngine:
        self.dsns.append(dsn)
        return self.engine.as_engine()
