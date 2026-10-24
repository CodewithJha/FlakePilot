"""Browser-evidence adapters: a recorded fixture and the Browserbase Fetch API.

`BrowserbaseEvidence` speaks Browserbase's REST Fetch endpoint
(`POST https://api.browserbase.com/v1/fetch`, `X-BB-API-Key`), which
retrieves a page through Browserbase's infrastructure without executing
JavaScript and without a browser session. That is the whole no-dependency
path: driving a real browser session needs a CDP WebSocket client (the
session's `connectUrl`), which this repository does not ship. Because Fetch
holds no remote session, there is nothing to release; the `session_id` of a
capture is local and each page's `capture_id` is Browserbase's fetch id.

Every target is checked against the operator's allowlist before any contact.
The adapters take plain host strings and build the port's `BrowserAllowlist`
themselves, so the composition root never imports the application layer.
Redirects are refused at both hops: Browserbase is asked not to follow them
(`allowRedirects: false`) and a 3xx target status is a refusal, and the call to
Browserbase itself never follows one. The API key rides only in its header;
reasons are short codes and non-2xx bodies are never read.
"""

import asyncio
import hashlib
import http.client
import json
import re
import ssl
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from flakepilot.integrations.application.browser import (
    BrowserAllowlist,
    BrowserCapture,
    BrowserCaptureRequest,
    BrowserEvidence,
    BrowserEvidenceError,
    BrowserRefusal,
)
from flakepilot.integrations.application.ports import AiCapabilities

PROVIDER = "browserbase"
RECORDED_PROVIDER = "recorded_browser"
FETCH_URL = "https://api.browserbase.com/v1/fetch"
# Browserbase caps Fetch content at 5 MB; JSON escaping can grow it further.
MAX_RESPONSE_BYTES = 12 * 1024 * 1024

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_transport(
    request: urllib.request.Request, timeout: float
) -> tuple[int, bytes]:
    """Blocking call with a socket timeout; run it off the event loop."""
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, b""  # a non-2xx body is never read


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> str:
    return uuid.uuid4().hex


class BrowserbaseEvidence:
    """Allowlisted, budgeted page capture through Browserbase Fetch.

    Provider-level failures (bad key, rate limit, Browserbase down, malformed
    answer) raise `BrowserEvidenceError`; target-level outcomes (off the
    allowlist, redirected, non-200, binary, too large, target timeout) become
    refusals so the rest of the session still yields what it can.
    """

    def __init__(
        self,
        *,
        api_key: str,
        allowed_hosts: Iterable[str],
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] = _utcnow,
        new_session_id: Callable[[], str] = _new_session_id,
    ) -> None:
        if not api_key or not api_key.isprintable() or len(api_key) > 200:
            raise ValueError("the Browserbase API key is 1-200 printable characters")
        self._key = api_key
        self._allowlist = BrowserAllowlist(frozenset(allowed_hosts))
        self._transport = transport
        self._clock = clock
        self._new_session_id = new_session_id

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    async def capture(self, request: BrowserCaptureRequest) -> BrowserCapture:
        session_id = self._new_session_id()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        evidence: list[BrowserEvidence] = []
        refused: list[BrowserRefusal] = []
        for url in request.targets:
            remaining = deadline - loop.time()
            if not self._allowlist.permits(url):
                refused.append(BrowserRefusal(url, "off_allowlist"))
            elif remaining <= 0:
                refused.append(BrowserRefusal(url, "budget_exhausted"))
            else:
                outcome = await self._fetch(
                    url, session_id, remaining, request.max_page_bytes
                )
                if isinstance(outcome, BrowserEvidence):
                    evidence.append(outcome)
                else:
                    refused.append(outcome)
        return BrowserCapture(
            session_id=session_id, evidence=tuple(evidence), refused=tuple(refused)
        )

    async def _fetch(
        self, url: str, session_id: str, remaining: float, max_bytes: int
    ) -> BrowserEvidence | BrowserRefusal:
        body = json.dumps(
            {
                "url": url,
                "allowRedirects": False,
                "allowInsecureSsl": False,
                "proxies": False,
            }
        ).encode()
        call = urllib.request.Request(
            FETCH_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "FlakePilot",
                "X-BB-API-Key": self._key,
            },
            method="POST",
        )
        try:
            status, raw = await asyncio.wait_for(
                asyncio.to_thread(self._transport, call, remaining), timeout=remaining
            )
        except TimeoutError:
            return BrowserRefusal(url, "timeout")
        except (OSError, http.client.HTTPException):
            # Raised below, outside the handler, so no transport text is chained.
            status, raw = 0, b""
        if status == 0:
            raise BrowserEvidenceError("provider_unreachable", unavailable=True)
        if status in (401, 403):
            raise BrowserEvidenceError("unauthorized")
        if status == 429:
            raise BrowserEvidenceError("rate_limited", unavailable=True)
        if status in (502, 504):
            # Browserbase's documented target failures: too large, TLS, slow.
            return BrowserRefusal(url, "target_unavailable")
        if status in (400, 422):
            return BrowserRefusal(url, "target_rejected")
        if status != 200:
            raise BrowserEvidenceError("provider_http_error", unavailable=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BrowserEvidenceError("response_too_large", unavailable=True)
        answer = _parse(raw)
        target_status = answer["statusCode"]
        if 300 <= target_status <= 399:
            return BrowserRefusal(url, "redirect_refused")
        if target_status != 200:
            return BrowserRefusal(url, "target_http_error")
        if answer["encoding"] != "utf-8":
            return BrowserRefusal(url, "binary_content")
        page = answer["content"].encode()
        if len(page) > max_bytes:
            return BrowserRefusal(url, "too_large")
        return BrowserEvidence.of(
            url=url,
            session_id=session_id,
            capture_id=answer["id"],
            retrieved_at=self._clock(),
            status=target_status,
            content_type=answer["contentType"],
            body=page,
        )


def _parse(raw: bytes) -> dict[str, Any]:
    """The documented Fetch answer, shape-checked; anything else fails closed."""
    try:
        answer = json.loads(raw)
    except ValueError:
        raise BrowserEvidenceError("bad_response", unavailable=True) from None
    if (
        not isinstance(answer, dict)
        or not isinstance(answer.get("id"), str)
        or _ID.fullmatch(answer["id"]) is None
        or type(answer.get("statusCode")) is not int
        or not isinstance(answer.get("content"), str)
        or not isinstance(answer.get("encoding"), str)
        or not isinstance(answer.get("contentType"), str)
        or not 1 <= len(answer["contentType"]) <= 100
        or not answer["contentType"].isprintable()
    ):
        raise BrowserEvidenceError("bad_response", unavailable=True)
    return answer


class RecordedBrowserEvidence:
    """Deterministic, credential-free capture over canned pages keyed by URL.

    Same allowlist and byte-cap rules as the live adapter; nothing leaves the
    process. Session and capture ids derive from the targets and URLs.
    """

    def __init__(
        self,
        pages: Mapping[str, tuple[str, str]],
        *,
        allowed_hosts: Iterable[str],
        retrieved_at: datetime,
    ) -> None:
        """`pages` maps a URL to `(content_type, text)`."""
        self._pages = dict(pages)
        self._allowlist = BrowserAllowlist(frozenset(allowed_hosts))
        self._retrieved_at = retrieved_at

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=RECORDED_PROVIDER)

    async def capture(self, request: BrowserCaptureRequest) -> BrowserCapture:
        session_id = _digest("\n".join(request.targets))
        evidence: list[BrowserEvidence] = []
        refused: list[BrowserRefusal] = []
        for url in request.targets:
            page = self._pages.get(url)
            if not self._allowlist.permits(url):
                refused.append(BrowserRefusal(url, "off_allowlist"))
            elif page is None:
                refused.append(BrowserRefusal(url, "not_recorded"))
            elif len(page[1].encode()) > request.max_page_bytes:
                refused.append(BrowserRefusal(url, "too_large"))
            else:
                evidence.append(
                    BrowserEvidence.of(
                        url=url,
                        session_id=session_id,
                        capture_id=_digest(url),
                        retrieved_at=self._retrieved_at,
                        status=200,
                        content_type=page[0],
                        body=page[1].encode(),
                    )
                )
        return BrowserCapture(
            session_id=session_id, evidence=tuple(evidence), refused=tuple(refused)
        )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]
