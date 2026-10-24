"""Browser-evidence port and adapters (ADR-017 decision 8; goal 3).

An injected transport stands in for Browserbase: every test runs the real
adapter over a canned HTTP exchange. The allowlist is checked before contact,
redirects are refused, failures are short codes, and the key rides only in
its header.
"""

import asyncio
import hashlib
import http.client
import json
import time
import urllib.request
from datetime import UTC, datetime

import pytest

from flakepilot.integrations.application.browser import (
    BrowserAllowlist,
    BrowserCaptureRequest,
    BrowserEvidence,
    BrowserEvidenceError,
    target_host,
)
from flakepilot.integrations.infrastructure.browserbase import (
    FETCH_URL,
    PROVIDER,
    BrowserbaseEvidence,
    RecordedBrowserEvidence,
)

KEY = "bb_live_secret_key_123"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DOCS = "https://docs.python.org/3/library/asyncio.html"
HOSTS = frozenset({"docs.python.org", "github.com"})
ALLOW = BrowserAllowlist(HOSTS)
PAGE = "<html><body>asyncio docs</body></html>"


class RecordedTransport:
    """Canned answers, one per call, recording each request."""

    def __init__(self, *answers: tuple[int, bytes] | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetched(
    content: str = PAGE,
    *,
    status: int = 200,
    encoding: str = "utf-8",
    fetch_id: str = "f-123",
) -> tuple[int, bytes]:
    return 200, json.dumps(
        {
            "id": fetch_id,
            "statusCode": status,
            "headers": {"content-type": "text/html"},
            "content": content,
            "contentType": "text/html; charset=utf-8",
            "encoding": encoding,
        }
    ).encode()


def _adapter(transport: RecordedTransport) -> BrowserbaseEvidence:
    return BrowserbaseEvidence(
        api_key=KEY,
        allowed_hosts=HOSTS,
        transport=transport,
        clock=lambda: NOW,
        new_session_id=lambda: "session-1",
    )


def _capture(transport: RecordedTransport, *targets: str, **kwargs: int):  # type: ignore[no-untyped-def]
    return asyncio.run(
        _adapter(transport).capture(BrowserCaptureRequest(targets, **kwargs))
    )


def _no_key_anywhere(error: BaseException) -> None:
    chain: BaseException | None = error
    while chain is not None:
        assert KEY not in str(chain) and KEY not in repr(chain)
        chain = chain.__cause__ or chain.__context__


# --- the allowlist and target shapes -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/",  # not https
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.0.0.8/admin",
        "https://localhost/",
        "https://metadata.internal/",
        "https://printer.local/",
        "https://user:pass@docs.python.org/",
        "https://docs.python.org:8443/",
        "https://docs.python.org\\@evil.com/",
        "https://docs.python.org/ path",
        "ftp://docs.python.org/",
        "",
    ],
)
def test_uncapturable_urls_have_no_host(url: str) -> None:
    assert target_host(url) is None


def test_a_public_https_url_has_its_host() -> None:
    assert target_host(DOCS) == "docs.python.org"
    assert target_host("https://docs.python.org:443/x") == "docs.python.org"


def test_the_allowlist_is_an_exact_host_match() -> None:
    assert ALLOW.permits(DOCS)
    assert not ALLOW.permits("https://evil.docs.python.org/")
    assert not ALLOW.permits("https://docs.python.org.evil.com/")
    assert not ALLOW.permits("https://pypi.org/")


@pytest.mark.parametrize(
    "hosts",
    [
        frozenset(),
        frozenset({"localhost"}),
        frozenset({"10.0.0.1"}),
        frozenset({"x.local"}),
    ],
)
def test_the_allowlist_refuses_unusable_hosts(hosts: frozenset[str]) -> None:
    with pytest.raises(ValueError):
        BrowserAllowlist(hosts)


def test_the_request_shape_is_bounded() -> None:
    with pytest.raises(ValueError):
        BrowserCaptureRequest(())
    with pytest.raises(ValueError):
        BrowserCaptureRequest(tuple(f"https://github.com/{n}" for n in range(6)))
    with pytest.raises(ValueError):
        BrowserCaptureRequest((DOCS, DOCS))
    with pytest.raises(ValueError):
        BrowserCaptureRequest((DOCS,), timeout_seconds=301)
    with pytest.raises(ValueError):
        BrowserCaptureRequest((DOCS,), max_page_bytes=6 * 1024 * 1024)


# --- the Browserbase adapter ----------------------------------------------


def test_a_page_becomes_evidence_with_provenance() -> None:
    transport = RecordedTransport(_fetched())
    capture = _capture(transport, DOCS)
    assert capture.session_id == "session-1" and capture.refused == ()
    (evidence,) = capture.evidence
    assert evidence.url == DOCS and evidence.capture_id == "f-123"
    assert evidence.session_id == "session-1" and evidence.retrieved_at == NOW
    assert evidence.sha256 == hashlib.sha256(PAGE.encode()).hexdigest()
    assert evidence.size == len(PAGE.encode()) and evidence.excerpt == PAGE
    call = transport.requests[0]
    assert call.full_url == FETCH_URL and call.get_method() == "POST"
    assert call.get_header("X-bb-api-key") == KEY
    sent = json.loads(call.data.decode())  # type: ignore[union-attr]
    assert sent == {
        "url": DOCS,
        "allowRedirects": False,
        "allowInsecureSsl": False,
        "proxies": False,
    }
    assert KEY not in call.data.decode()  # type: ignore[union-attr]


def test_off_allowlist_targets_are_refused_without_contact() -> None:
    transport = RecordedTransport()
    capture = _capture(
        transport,
        "https://169.254.169.254/latest",
        "https://pypi.org/",
        "https://localhost/",
    )
    assert [refusal.reason for refusal in capture.refused] == ["off_allowlist"] * 3
    assert capture.evidence == () and transport.requests == []


def test_a_redirected_target_is_refused_not_followed() -> None:
    transport = RecordedTransport(_fetched("", status=302))
    capture = _capture(transport, DOCS)
    assert capture.evidence == ()
    assert capture.refused[0].reason == "redirect_refused"
    assert len(transport.requests) == 1


def test_target_level_failures_are_refusals_and_the_session_continues() -> None:
    other = "https://github.com/python/cpython"
    transport = RecordedTransport((502, b""), _fetched(fetch_id="f-2"))
    capture = _capture(transport, DOCS, other)
    assert capture.refused[0].reason == "target_unavailable"
    assert [item.url for item in capture.evidence] == [other]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_fetched(status=404), "target_http_error"),
        (_fetched(encoding="base64"), "binary_content"),
        ((400, b""), "target_rejected"),
    ],
)
def test_unusable_targets_are_refused(answer: tuple[int, bytes], reason: str) -> None:
    capture = _capture(RecordedTransport(answer), DOCS)
    assert capture.evidence == () and capture.refused[0].reason == reason


def test_an_oversized_page_is_refused() -> None:
    capture = _capture(RecordedTransport(_fetched("x" * 101)), DOCS, max_page_bytes=100)
    assert capture.refused[0].reason == "too_large"


def test_the_excerpt_is_capped_but_the_hash_covers_the_page() -> None:
    page = "y" * 20_000
    (evidence,) = _capture(RecordedTransport(_fetched(page)), DOCS).evidence
    assert len(evidence.excerpt.encode()) == 8 * 1024
    assert evidence.size == 20_000
    assert evidence.sha256 == hashlib.sha256(page.encode()).hexdigest()


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_fails_the_session_without_echoing_it(status: int) -> None:
    with pytest.raises(BrowserEvidenceError) as caught:
        _capture(RecordedTransport((status, b"")), DOCS)
    assert caught.value.reason == "unauthorized" and not caught.value.unavailable
    _no_key_anywhere(caught.value)


def test_rate_limiting_is_unavailable() -> None:
    with pytest.raises(BrowserEvidenceError) as caught:
        _capture(RecordedTransport((429, b"")), DOCS)
    assert caught.value.reason == "rate_limited" and caught.value.unavailable


def test_provider_errors_are_unavailable() -> None:
    with pytest.raises(BrowserEvidenceError) as caught:
        _capture(RecordedTransport((500, b"stack trace with secrets")), DOCS)
    assert caught.value.reason == "provider_http_error"
    assert "stack" not in str(caught.value)


def test_an_unreachable_provider_fails_closed_without_the_key() -> None:
    for failure in (
        ConnectionError(f"refused {KEY}"),
        http.client.IncompleteRead(KEY.encode()),
    ):
        with pytest.raises(BrowserEvidenceError) as caught:
            _capture(RecordedTransport(failure), DOCS)
        assert caught.value.reason == "provider_unreachable"
        _no_key_anywhere(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b"[]",
        json.dumps(
            {
                "id": "f",
                "statusCode": "200",
                "content": "",
                "contentType": "t",
                "encoding": "utf-8",
            }
        ).encode(),
        json.dumps(
            {
                "id": "bad id!",
                "statusCode": 200,
                "content": "",
                "contentType": "t",
                "encoding": "utf-8",
            }
        ).encode(),
        json.dumps(
            {
                "id": "f",
                "statusCode": 200,
                "content": {"a": 1},
                "contentType": "t",
                "encoding": "utf-8",
            }
        ).encode(),
    ],
)
def test_malformed_answers_fail_closed(body: bytes) -> None:
    with pytest.raises(BrowserEvidenceError) as caught:
        _capture(RecordedTransport((200, body)), DOCS)
    assert caught.value.reason == "bad_response"


def test_a_slow_target_times_out_and_the_budget_refuses_the_rest() -> None:
    def slow(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        time.sleep(1.5)
        return _fetched()

    adapter = BrowserbaseEvidence(
        api_key=KEY, allowed_hosts=HOSTS, transport=slow, new_session_id=lambda: "s"
    )
    capture = asyncio.run(
        adapter.capture(
            BrowserCaptureRequest((DOCS, "https://github.com/x"), timeout_seconds=1)
        )
    )
    assert [refusal.reason for refusal in capture.refused] == [
        "timeout",
        "budget_exhausted",
    ]
    assert capture.evidence == ()


def test_the_transport_timeout_never_exceeds_the_session_budget() -> None:
    transport = RecordedTransport(_fetched())
    _capture(transport, DOCS, timeout_seconds=7)
    assert 0 < transport.timeouts[0] <= 7


def test_an_error_mid_session_stops_contact_and_holds_no_session() -> None:
    """Fetch holds no remote session, so there is nothing to release; the
    only cleanup obligation is to stop contacting the provider."""
    transport = RecordedTransport(_fetched(), (401, b""), _fetched())
    with pytest.raises(BrowserEvidenceError):
        _capture(transport, DOCS, "https://github.com/a", "https://github.com/b")
    assert len(transport.requests) == 2
    assert all(call.full_url == FETCH_URL for call in transport.requests)


def test_constructor_refuses_an_unusable_key_or_host() -> None:
    with pytest.raises(ValueError):
        BrowserbaseEvidence(api_key="", allowed_hosts=HOSTS)
    with pytest.raises(ValueError):
        BrowserbaseEvidence(api_key="a\nb", allowed_hosts=HOSTS)
    with pytest.raises(ValueError):
        BrowserbaseEvidence(api_key=KEY, allowed_hosts={"169.254.169.254"})


def test_describe_names_the_provider() -> None:
    assert _adapter(RecordedTransport()).describe().provider == PROVIDER


# --- the recorded fixture -------------------------------------------------


def test_the_fixture_is_deterministic_and_obeys_the_same_rules() -> None:
    fixture = RecordedBrowserEvidence(
        {DOCS: ("text/html", PAGE), "https://pypi.org/": ("text/html", "x")},
        allowed_hosts=HOSTS,
        retrieved_at=NOW,
    )
    request = BrowserCaptureRequest((DOCS, "https://pypi.org/", "https://github.com/x"))
    first = asyncio.run(fixture.capture(request))
    assert first == asyncio.run(fixture.capture(request))
    assert [item.url for item in first.evidence] == [DOCS]
    assert [(item.url, item.reason) for item in first.refused] == [
        ("https://pypi.org/", "off_allowlist"),
        ("https://github.com/x", "not_recorded"),
    ]


def test_evidence_refuses_a_non_capturable_source() -> None:
    with pytest.raises(ValueError):
        BrowserEvidence.of(
            url="https://127.0.0.1/",
            session_id="s",
            capture_id="c",
            retrieved_at=NOW,
            status=200,
            content_type="text/html",
            body=b"x",
        )
    with pytest.raises(ValueError):
        BrowserEvidence.of(
            url=DOCS,
            session_id="s",
            capture_id="c",
            retrieved_at=datetime(2026, 1, 1),  # naive
            status=200,
            content_type="text/html",
            body=b"x",
        )
