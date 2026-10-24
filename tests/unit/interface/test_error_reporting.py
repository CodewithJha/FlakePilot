"""Opt-in error reporting: what leaves the process, captured at the transport."""

import asyncio
import json
import logging
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from support import SETTINGS, send_raw

from flakepilot.interface.composition_root import Secret, create_app
from flakepilot.interface.composition_root.error_reporting import (
    HANDLER_NAME,
    configure_error_reporting,
)
from flakepilot.interface.http.request_context import RequestContextMiddleware

DSN = "https://0123abcdpublic@o42.ingest.example.invalid/4508"
API_KEY = "sk-live-FAKEKEY-9f8e7d"
COOKIE = "c00kie-FAKE-value-77"
BEARER = "fpi_FAKEbearerTOKEN123"
QUERY = "q-FAKEquery-secret"
BODY = "body-FAKE-secret"
SECRETS = (API_KEY, COOKIE, BEARER, QUERY, BODY)
EVENT_KEYS = {
    "event_id",
    "timestamp",
    "platform",
    "level",
    "logger",
    "release",
    "tags",
    "logentry",
    "exception",
}


class Captured:
    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> None:
        self.requests.append(request)

    def wire(self) -> str:
        """Everything that would have left the process, as one string."""
        parts = []
        for request in self.requests:
            assert isinstance(request.data, bytes)
            parts += [request.full_url, repr(request.header_items())]
            parts.append(request.data.decode())
        return "\n".join(parts)

    def events(self) -> list[dict[str, Any]]:
        events = []
        for request in self.requests:
            assert isinstance(request.data, bytes)
            header, item, rest = request.data.split(b"\n", 2)
            length = json.loads(item)["length"]
            assert json.loads(item)["type"] == "event"
            assert rest[length:] == b"\n"
            event = json.loads(rest[:length])
            assert json.loads(header)["event_id"] == event["event_id"]
            events.append(event)
        return events


@pytest.fixture
def captured() -> Iterator[Captured]:
    transport = Captured()
    configure_error_reporting(replace(SETTINGS, sentry_dsn=Secret(DSN)), transport)
    yield transport
    configure_error_reporting(SETTINGS)


def drain() -> None:
    """Remove the reporter; closing it sends everything queued."""
    configure_error_reporting(SETTINGS)


def reporters() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME]


def fail_with_secrets() -> None:
    api_key = API_KEY  # a local variable that must never be exported
    raise RuntimeError(f"upstream refused key={api_key} cookie={COOKIE}")


def test_no_dsn_installs_nothing() -> None:
    configure_error_reporting(SETTINGS)
    create_app(SETTINGS)
    assert reporters() == []


def test_the_envelope_goes_to_the_dsn_project_with_the_public_key_only(
    captured: Captured,
) -> None:
    assert len(reporters()) == 1
    logging.getLogger("flakepilot.test").error("worker stopped: %s", "OSError")
    drain()
    [request] = captured.requests
    assert request.full_url == "https://o42.ingest.example.invalid/api/4508/envelope/"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/x-sentry-envelope"
    auth = request.get_header("X-sentry-auth")
    assert auth is not None and auth.startswith("Sentry sentry_version=7, ")
    assert "sentry_key=0123abcdpublic," in auth
    [event] = captured.events()
    assert event["logentry"] == {"message": "worker stopped: %s"}
    assert event["level"] == "error"


def test_logged_secrets_never_leave_the_process(captured: Captured) -> None:
    log = logging.getLogger("flakepilot.test")
    try:
        fail_with_secrets()
    except RuntimeError:
        log.exception(
            "call failed for %s with %s",
            BEARER,
            API_KEY,
            extra={
                "route": "/orgs/{org_id}/runs/{run_id}",
                "org_id": "org_1",
                "repo_id": BEARER,
                "password": API_KEY,
            },
        )
    log.error(f"Authorization: Bearer {BEARER}")
    log.info("below the reporting level %s", API_KEY)
    drain()

    wire = captured.wire()
    for secret in SECRETS:
        assert secret not in wire
    assert "api_key" not in wire  # the local variable's name, let alone its value
    first, second = captured.events()
    assert set(first) <= EVENT_KEYS
    assert first["logentry"] == {"message": "call failed for %s with %s"}
    assert first["tags"] == {"route": "/orgs/{org_id}/runs/{run_id}", "org_id": "org_1"}
    [exception] = first["exception"]["values"]
    assert (exception["type"], exception["module"]) == ("RuntimeError", "builtins")
    assert "value" not in exception
    frame = exception["stacktrace"]["frames"][-1]
    assert set(frame) == {"filename", "function", "lineno", "in_app"}
    assert (frame["filename"], frame["function"]) == (
        "test_error_reporting.py",
        "fail_with_secrets",
    )
    assert second["logentry"] == {"message": "[redacted]"}


def test_request_data_never_leaves_and_events_carry_the_request_id(
    captured: Captured,
) -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.post("/boom/{item}")
    async def boom(item: str) -> None:
        raise RuntimeError(f"{item} {API_KEY}")

    status, headers, _ = asyncio.run(
        send_raw(
            app,
            "POST",
            f"/boom/{BEARER}?api_key={QUERY}",
            headers=[
                ("Cookie", f"__Host-flakepilot_session={COOKIE}"),
                ("Authorization", f"Bearer {BEARER}"),
                ("X-Request-ID", "client-chosen-1"),
                ("Content-Type", "application/json"),
            ],
            body=json.dumps({"token": BODY}).encode(),
        )
    )
    drain()

    assert status == 500
    wire = captured.wire()
    for secret in SECRETS:
        assert secret not in wire
    for absent in ("client-chosen-1", "Cookie", "cookie", "127.0.0.1", "testserver"):
        assert absent not in wire
    [event] = captured.events()
    assert set(event) <= EVENT_KEYS
    assert event["tags"] == {
        "request_id": dict(headers)["x-request-id"],
        "route": "/boom/{item}",
    }
    assert event["exception"]["values"][0]["type"] == "RuntimeError"


def test_a_failing_transport_drops_events_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(request: urllib.request.Request) -> None:
        raise OSError("network unreachable")

    configure_error_reporting(replace(SETTINGS, sentry_dsn=Secret(DSN)), refuse)
    logging.getLogger("flakepilot.test").error("first")
    drain()
    assert [r.getMessage() for r in caplog.records] == ["first"]
    assert reporters() == []
