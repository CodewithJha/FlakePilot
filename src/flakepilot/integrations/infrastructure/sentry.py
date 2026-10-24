"""Sentry envelope transport (sponsor goal 7), standard library only.

Posts one already allow-listed event per request to the DSN's envelope endpoint
(`https://<host>[/<prefix>]/api/<project>/envelope/`), authenticated by the DSN's
public key in `X-Sentry-Auth`. TLS is verified, redirects are refused, the call
is bounded in time, and the response body is ignored. What goes into an event
is decided by the composition root's error reporter, never here.
"""

import json
import ssl
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit

TIMEOUT = 5.0

Post = Callable[[urllib.request.Request], None]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_post(request: urllib.request.Request) -> None:
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    with opener.open(request, timeout=TIMEOUT) as response:
        response.read(1024)


def envelope(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, separators=(",", ":")).encode()
    header = {"event_id": event["event_id"], "sent_at": datetime.now(UTC).isoformat()}
    item = {"type": "event", "length": len(payload)}
    lines = [
        json.dumps(part, separators=(",", ":")).encode() for part in (header, item)
    ]
    return b"\n".join([*lines, payload]) + b"\n"


class SentryEnvelopeTransport:
    def __init__(self, dsn: str, post: Post = urllib_post) -> None:
        parts = urlsplit(dsn)
        prefix, _, project = parts.path.rpartition("/")
        host = parts.netloc.rpartition("@")[2]
        self._url = f"https://{host}{prefix}/api/{project}/envelope/"
        self._auth = (
            f"Sentry sentry_version=7, sentry_key={parts.username}, "
            f"sentry_client=flakepilot/{version('flakepilot')}"
        )
        self._post = post

    def __call__(self, event: dict[str, Any]) -> None:
        self._post(
            urllib.request.Request(
                self._url,
                data=envelope(event),
                method="POST",
                headers={
                    "Content-Type": "application/x-sentry-envelope",
                    "X-Sentry-Auth": self._auth,
                },
            )
        )
