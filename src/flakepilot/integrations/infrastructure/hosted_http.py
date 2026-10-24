"""Bounded HTTPS exchange for hosted providers (ADR-017, goals 1 and 8).

One request, one capped answer: https only (plain http only to a loopback
host), no redirects, a socket timeout on every attempt, a byte cap on every
body, and a short reason code on every failure. Retries are bounded and only
for answers that may succeed later (408, 429, 5xx); every other status is
final. Nothing here logs, and no exception carries a URL, header, or body:
some providers put the API key in the URL.

A non-2xx body is read only up to `ERROR_BODY_BYTES`, so an adapter can tell
a known machine-readable code apart (an invalid key, an exhausted quota). It is
never echoed into a reason, exception, or log line.
"""

import asyncio
import http.client
import ipaddress
import random
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

ERROR_BODY_BYTES = 16 * 1024
MAX_ATTEMPTS = 3
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

# (request, per-attempt timeout in seconds, response byte cap) -> (status, body)
Transport = Callable[[urllib.request.Request, float, int], tuple[int, bytes]]
Sleep = Callable[[float], Awaitable[None]]


class HostedError(Exception):
    """A short reason code. Carries no URL, header, or provider text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


def endpoint_ok(url: str) -> bool:
    """https anywhere; plain http only to a loopback host. No credentials,
    query or fragment: a key never rides in a configured URL."""
    if not 1 <= len(url) <= 500:
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    if not parts.hostname or "@" in parts.netloc or parts.query or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and _loopback(parts.hostname)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect is not an answer; the 3xx status is final


class _Reader(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def _read(stream: _Reader, cap: int) -> bytes:
    body = stream.read(cap + 1)
    if len(body) > cap:
        raise HostedError("response_too_large")
    return body


def https_transport(
    request: urllib.request.Request, timeout_seconds: float, max_bytes: int
) -> tuple[int, bytes]:
    """Blocking call; run it off the event loop."""
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return response.status, _read(response, max_bytes)
    except urllib.error.HTTPError as error:
        with error:
            try:
                return error.code, error.read(ERROR_BODY_BYTES)
            except (OSError, http.client.HTTPException):
                return error.code, b""
    except TimeoutError:
        raise HostedError("provider_timeout") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise HostedError("provider_timeout") from None
        raise HostedError("provider_unreachable") from None
    except (OSError, http.client.HTTPException):
        raise HostedError("provider_unreachable") from None


CALL_MAX_BYTES = 4 * 1024 * 1024


def call(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    """`https_transport` for callers that hold no urllib types (the TabPFN
    predictor). https only; every failure is an `OSError` carrying a reason code."""
    if urlsplit(url).scheme != "https":
        raise OSError("not_https")
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        return https_transport(request, timeout_seconds, CALL_MAX_BYTES)
    except HostedError as error:
        raise OSError(error.reason) from None


def retryable(status: int, body: bytes) -> bool:
    del body
    return status in _RETRYABLE


async def exchange(
    request: urllib.request.Request,
    *,
    transport: Transport,
    timeout_seconds: float,
    max_bytes: int,
    sleep: Sleep = asyncio.sleep,
    retry: Callable[[int, bytes], bool] = retryable,
    base_delay: float = 0.5,
) -> tuple[int, bytes]:
    """The last (status, body) after at most `MAX_ATTEMPTS` attempts; exponential
    backoff with jitter between them. Transport failures (timeout, unreachable,
    oversized) raise `HostedError` at once: they are not retried."""
    for attempt in range(MAX_ATTEMPTS):
        call = asyncio.to_thread(transport, request, timeout_seconds, max_bytes)
        try:
            # The socket timeout bounds each read; this bounds the whole attempt.
            status, body = await asyncio.wait_for(call, timeout_seconds + 1)
        except HostedError:
            raise
        except TimeoutError:
            raise HostedError("provider_timeout") from None
        except (OSError, http.client.HTTPException):
            raise HostedError("provider_unreachable") from None
        if attempt == MAX_ATTEMPTS - 1 or not retry(status, body):
            return status, body
        await sleep(base_delay * 2**attempt * (1 + random.random() / 2))
    raise AssertionError("unreachable")  # the loop always returns
