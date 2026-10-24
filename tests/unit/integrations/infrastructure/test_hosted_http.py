"""The bounded HTTPS exchange shared by hosted providers (goals 1 and 8).

The transport tests run the real urllib path against a throwaway loopback
server: caps, refused redirects, socket timeouts, and error bodies. The retry
tests drive `exchange` with a scripted transport and a recording sleep.
"""

import asyncio
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flakepilot.integrations.infrastructure.hosted_http import (
    ERROR_BODY_BYTES,
    MAX_ATTEMPTS,
    HostedError,
    endpoint_ok,
    exchange,
    https_transport,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/big":
            self._send(200, b"x" * 2048)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(1.5)
            self._send(200, b"late")
        elif self.path == "/error":
            self._send(401, b"e" * (ERROR_BODY_BYTES * 2))
        else:
            self._send(200, b"ok")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, method="GET")


def test_endpoint_rule_is_https_or_loopback_http() -> None:
    assert endpoint_ok("https://generativelanguage.googleapis.com/v1beta")
    assert endpoint_ok("http://127.0.0.1:8080/v1beta")
    assert endpoint_ok("http://localhost:8080")
    assert endpoint_ok("http://[::1]:8080")
    assert not endpoint_ok("http://generativelanguage.googleapis.com/v1beta")
    assert not endpoint_ok("ftp://example.test")
    assert not endpoint_ok("https://user:pw@example.test")
    assert not endpoint_ok("https://example.test/v1?key=abc")
    assert not endpoint_ok("https://example.test/#frag")
    assert not endpoint_ok("https://")
    assert not endpoint_ok("https://example.test:99999")
    assert not endpoint_ok("https://example.test/" + "a" * 600)


def test_a_small_answer_is_returned(server: str) -> None:
    assert https_transport(_get(f"{server}/ok"), 5, 1024) == (200, b"ok")


def test_a_body_over_the_cap_is_refused(server: str) -> None:
    with pytest.raises(HostedError) as error:
        https_transport(_get(f"{server}/big"), 5, 1024)
    assert error.value.reason == "response_too_large"


def test_a_redirect_is_not_followed(server: str) -> None:
    status, _ = https_transport(_get(f"{server}/redirect"), 5, 1024)
    assert status == 302


def test_a_slow_answer_is_a_timeout(server: str) -> None:
    with pytest.raises(HostedError) as error:
        https_transport(_get(f"{server}/slow"), 0.3, 1024)
    assert error.value.reason == "provider_timeout"


def test_an_error_body_is_read_only_up_to_its_cap(server: str) -> None:
    status, body = https_transport(_get(f"{server}/error"), 5, 1024)
    assert status == 401 and len(body) == ERROR_BODY_BYTES


def test_an_unreachable_host_is_a_short_code() -> None:
    with pytest.raises(HostedError) as error:
        https_transport(_get("http://127.0.0.1:1/"), 2, 1024)
    assert error.value.reason == "provider_unreachable"
    assert str(error.value) == "provider_unreachable"
    assert error.value.__cause__ is None and error.value.__suppress_context__


class Scripted:
    def __init__(self, *answers: tuple[int, bytes] | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(
        self, request: urllib.request.Request, timeout: float, cap: int
    ) -> tuple[int, bytes]:
        del request, timeout, cap
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _exchange(transport: Scripted, sleeps: Sleeps) -> tuple[int, bytes]:
    return asyncio.run(
        exchange(
            _get("https://example.test/"),
            transport=transport,
            timeout_seconds=5,
            max_bytes=1024,
            sleep=sleeps,
        )
    )


def test_a_transient_failure_is_retried_with_growing_backoff() -> None:
    transport, sleeps = Scripted((503, b""), (500, b""), (200, b"ok")), Sleeps()
    assert _exchange(transport, sleeps) == (200, b"ok")
    assert transport.calls == 3
    assert len(sleeps.delays) == 2 and sleeps.delays[0] < sleeps.delays[1]


def test_retries_are_bounded() -> None:
    transport, sleeps = Scripted((429, b"")), Sleeps()
    assert _exchange(transport, sleeps) == (429, b"")
    assert transport.calls == MAX_ATTEMPTS and len(sleeps.delays) == MAX_ATTEMPTS - 1


def test_a_final_status_is_not_retried() -> None:
    for status in (400, 401, 403, 404):
        transport, sleeps = Scripted((status, b"")), Sleeps()
        assert _exchange(transport, sleeps)[0] == status
        assert transport.calls == 1 and sleeps.delays == []


def test_transport_failures_are_not_retried() -> None:
    for raised, reason in (
        (HostedError("provider_timeout"), "provider_timeout"),
        (TimeoutError(), "provider_timeout"),
        (ConnectionError("refused"), "provider_unreachable"),
    ):
        transport, sleeps = Scripted(raised), Sleeps()
        with pytest.raises(HostedError) as error:
            _exchange(transport, sleeps)
        assert error.value.reason == reason and transport.calls == 1
