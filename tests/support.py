"""Test helpers: a direct ASGI caller, an in-memory stand-in for the database engine, and
throwaway PostgreSQL databases for integration tests."""

import asyncio
import hashlib
import json
import os
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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import Message

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError
from flakepilot.interface.composition_root import Secret, Settings

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
    engine = create_async_engine(
        make_url(url).set(drivername="postgresql+asyncpg"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@contextmanager
def throwaway_database(migrate: bool = False) -> Iterator[str]:
    """A fresh database next to FLAKEPILOT_TEST_DATABASE_URL, always dropped afterwards."""
    admin_url = os.environ["FLAKEPILOT_TEST_DATABASE_URL"]
    name = f"flakepilot_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        if migrate:
            environ = {
                k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")
            }
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=Path(__file__).resolve().parents[1],
                env={**environ, "FLAKEPILOT_DATABASE_URL": url},
                capture_output=True,
                timeout=120,
                check=True,
            )
        yield url
    finally:
        asyncio.run(
            admin_execute(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
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
