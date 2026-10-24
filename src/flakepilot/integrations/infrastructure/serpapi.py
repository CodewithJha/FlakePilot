"""Live research through SerpApi's Google Search API (ADR-017, goal 8).

`GET https://serpapi.com/search.json?engine=google&q=...&api_key=...`
(https://serpapi.com/search-api). Only `organic_results[].link/title/snippet`
are read. Google no longer honours a results-per-page parameter, so the
`max_sources` budget is applied here, after https-only filtering and URL
deduplication. Every kept source passes the `ResearchSource` contract and
carries this search's retrieval time; the provider identity is `describe()`.

The key is a query parameter by SerpApi's design, so the request URL is never
logged, echoed, or put in an exception. Results are untrusted data: titles and
snippets are cut to the port's byte caps but never interpreted, and provider
text (including its `error` message) never reaches a reason. 401/403 are
`provider_auth_failed`; a 429 that says the account ran out of searches is
`provider_quota_exhausted`, other 429s and 5xx are retried with bounded
backoff (`hosted_http.exchange`).
"""

import asyncio
import json
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    ResearchProviderError,
    ResearchQuery,
    ResearchSource,
)
from flakepilot.integrations.infrastructure.hosted_http import (
    HostedError,
    Sleep,
    Transport,
    exchange,
    https_transport,
    retryable,
)

PROVIDER = "serpapi"
ENDPOINT = "https://serpapi.com/search.json"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_URL = 2_048
_MAX_TITLE = 200
_MAX_EXCERPT = 8 * 1024
_KEY = re.compile(r"[A-Za-z0-9_-]{1,200}")


class SerpApiResearch:
    """One search per query; fail closed with a short code."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int = 30,
        transport: Transport = https_transport,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if _KEY.fullmatch(api_key) is None:
            raise ValueError("the SerpApi key is not a key")
        if not 1 <= int(timeout_seconds) <= 120:
            raise ValueError("the research timeout must be an integer from 1 to 120")
        self._key = api_key
        self._timeout_seconds = int(timeout_seconds)
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:
        return "SerpApiResearch()"

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    async def search(self, query: ResearchQuery) -> tuple[ResearchSource, ...]:
        parameters = {"engine": "google", "q": query.query, "api_key": self._key}
        request = urllib.request.Request(
            f"{ENDPOINT}?{urlencode(parameters)}",
            headers={"Accept": "application/json", "User-Agent": "FlakePilot"},
            method="GET",
        )
        try:
            status, body = await exchange(
                request,
                transport=self._transport,
                timeout_seconds=self._timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
                sleep=self._sleep,
                retry=_retry,
            )
        except HostedError as error:
            raise ResearchProviderError(error.reason, unavailable=True) from None
        if status != 200:
            raise _status_error(status, body)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ResearchProviderError("response_too_large", unavailable=True)
        return _sources(body, query.max_sources, self._clock())


def _out_of_searches(body: bytes) -> bool:
    """SerpApi answers 429 both for throughput and for an exhausted plan; only
    the `error` field tells them apart. It is matched, never echoed."""
    try:
        message = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(message, str) and "run out of searches" in message.lower()


def _retry(status: int, body: bytes) -> bool:
    return retryable(status, body) and not _out_of_searches(body)


def _status_error(status: int, body: bytes) -> ResearchProviderError:
    if status in (401, 403):
        return ResearchProviderError("provider_auth_failed")
    if status == 429 and _out_of_searches(body):
        return ResearchProviderError("provider_quota_exhausted")
    if status == 429:
        return ResearchProviderError("provider_rate_limited", unavailable=True)
    if status == 408 or status >= 500:
        return ResearchProviderError("provider_http_error", unavailable=True)
    return ResearchProviderError("provider_rejected")


def _sources(
    body: bytes, budget: int, retrieved_at: datetime
) -> tuple[ResearchSource, ...]:
    try:
        parsed = json.loads(body)
    except ValueError:
        raise ResearchProviderError("bad_response", unavailable=True) from None
    if not isinstance(parsed, dict):
        raise ResearchProviderError("bad_response", unavailable=True)
    metadata = parsed.get("search_metadata")
    if isinstance(metadata, dict) and metadata.get("status") == "Error":
        raise ResearchProviderError("provider_search_failed", unavailable=True)
    # A successful search with no results has no `organic_results` (and an
    # `error` saying so): an empty answer, not a failure.
    results = parsed.get("organic_results", [])
    if not isinstance(results, list):
        raise ResearchProviderError("bad_response", unavailable=True)
    seen: set[str] = set()
    sources: list[ResearchSource] = []
    for item in results:
        if len(sources) >= budget:
            break
        if not isinstance(item, dict):
            raise ResearchProviderError("bad_source")
        link, title = item.get("link"), item.get("title")
        snippet = item.get("snippet", "")
        if not (
            isinstance(link, str)
            and isinstance(title, str)
            and isinstance(snippet, str)
        ):
            raise ResearchProviderError("bad_source")
        if not _https_source(link) or link in seen:
            continue
        seen.add(link)
        try:
            sources.append(
                ResearchSource(
                    url=link,
                    title=_cut(title, _MAX_TITLE),
                    retrieved_at=retrieved_at,
                    excerpt=_cut(snippet, _MAX_EXCERPT),
                )
            )
        except ValueError:
            raise ResearchProviderError("bad_source") from None
    return tuple(sources)


def _https_source(url: str) -> bool:
    """A citable source: https, a host, no credentials, at most 2048 bytes.
    Anything else is skipped, not shipped."""
    if len(url.encode()) > _MAX_URL:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc


def _cut(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, never splitting a character."""
    return text.encode()[:limit].decode("utf-8", "ignore")
