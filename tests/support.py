"""Test helpers: a direct ASGI caller and an in-memory stand-in for the database engine."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message

from flakepilot.interface.composition_root import Secret, Settings

FAKE_PASSWORD = "Zx9-fake-pw-7Qk"
FAKE_DSN = f"postgresql://flakepilot:{FAKE_PASSWORD}@db.invalid:5432/flakepilot"
SETTINGS = Settings(Secret(FAKE_DSN))

Response = tuple[int, dict[str, str], Any]


async def request(
    app: FastAPI,
    method: str,
    target: str,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
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
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    content = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], response_headers, json.loads(content)


def get(
    app: FastAPI, target: str, headers: list[tuple[str, str]] | None = None
) -> Response:
    return asyncio.run(request(app, "GET", target, headers))


def is_uuid(value: str) -> bool:
    return str(uuid.UUID(value)) == value


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


class FakeEngineFactory:
    def __init__(self, engine: FakeEngine | None = None) -> None:
        self.engine = engine or FakeEngine()
        self.dsns: list[str] = []

    def __call__(self, dsn: str) -> AsyncEngine:
        self.dsns.append(dsn)
        return self.engine.as_engine()
