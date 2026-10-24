"""Browser-evidence port owned by INT (ADR-003; ADR-017 decision 8; goal 3).

A capture retrieves a few operator-allowlisted public pages as evidence. The
port, not the adapter, owns the rules every provider must obey:

- targets are `https` URLs whose host is an exact member of the operator's
  allowlist; IP literals, single-label names (`localhost`), private-use
  suffixes, credentials in the URL, and non-default ports are never
  allowlistable, so no capture can be pointed at an internal address;
- redirects are never followed: a redirected target is refused, not chased;
- one `capture` call is one bounded session with a wall-clock budget and a
  target cap; a provider that holds a remote session releases it in
  `finally`, whatever happens;
- every evidence record carries its source URL, retrieval time, session and
  capture ids, a SHA-256 of the retrieved bytes, their size, and a capped
  excerpt. Page content is untrusted data, never instructions, and carries no
  budget or authority.
"""

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from flakepilot.integrations.application.ports import AiCapabilities

MAX_TARGETS = 5
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_EXCERPT_BYTES = 8 * 1024
_MAX_URL = 2_048
_MAX_HOSTS = 50

_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A public DNS name: dot-separated LDH labels ending in an alphabetic TLD.
# Single labels (`localhost`) and dotted quads never match.
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_PRIVATE_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".arpa",
    ".test",
    ".invalid",
    ".example",
)


def _public_host(host: str) -> bool:
    if _HOST.fullmatch(host) is None or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def target_host(url: str) -> str | None:
    """The host of a capturable target URL, or None for any other shape.

    Capturable: `https`, a public DNS host, no credentials, default port,
    no whitespace, control characters, or backslashes that parsers disagree on.
    """
    if not url or len(url.encode()) > _MAX_URL:
        return None
    if any(char <= " " or char in "\\\x7f" for char in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme != "https" or host is None or port not in (None, 443):
        return None
    if parts.netloc.lower() not in (host, f"{host}:443"):
        return None  # userinfo or anything else riding beside the host
    return host if _public_host(host) else None


@dataclass(frozen=True)
class BrowserAllowlist:
    """The operator's exact host allowlist. Never derived from request input."""

    hosts: frozenset[str]

    def __post_init__(self) -> None:
        if not 1 <= len(self.hosts) <= _MAX_HOSTS:
            raise ValueError(f"a browser allowlist names 1-{_MAX_HOSTS} hosts")
        for host in self.hosts:
            if not isinstance(host, str) or not _public_host(host):
                raise ValueError("an allowlisted host is a public lowercase DNS name")

    def permits(self, url: str) -> bool:
        host = target_host(url)
        return host is not None and host in self.hosts


@dataclass(frozen=True)
class BrowserCaptureRequest:
    """One bounded capture session: targets, wall budget, per-page byte cap."""

    targets: tuple[str, ...]
    timeout_seconds: int = 60
    max_page_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not 1 <= len(self.targets) <= MAX_TARGETS:
            raise ValueError(f"a capture has 1-{MAX_TARGETS} targets")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("capture targets are unique")
        for target in self.targets:
            if not isinstance(target, str) or not 1 <= len(target.encode()) <= _MAX_URL:
                raise ValueError("a capture target is a URL of at most 2048 bytes")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("a capture budget is 1-300 seconds")
        if not 1 <= self.max_page_bytes <= MAX_PAGE_BYTES:
            raise ValueError("the per-page cap is 1 byte to 5 MiB")


@dataclass(frozen=True)
class BrowserEvidence:
    """One retrieved page with its provenance. `excerpt` is untrusted content."""

    url: str
    session_id: str
    capture_id: str
    retrieved_at: datetime
    status: int
    content_type: str
    sha256: str
    size: int
    excerpt: str

    def __post_init__(self) -> None:
        if target_host(self.url) is None:
            raise ValueError("evidence comes only from a capturable https URL")
        if (
            _ID.fullmatch(self.session_id) is None
            or _ID.fullmatch(self.capture_id) is None
        ):
            raise ValueError("session and capture ids are short opaque tokens")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at is timezone-aware")
        if not 200 <= self.status <= 299:
            raise ValueError("evidence is a successful retrieval")
        if (
            not 1 <= len(self.content_type) <= 100
            or not self.content_type.isprintable()
        ):
            raise ValueError("a content type is 1-100 printable characters")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("sha256 is 64 lowercase hex digits")
        if not 0 <= self.size <= MAX_PAGE_BYTES:
            raise ValueError("a page is at most 5 MiB")
        if len(self.excerpt.encode()) > MAX_EXCERPT_BYTES:
            raise ValueError("an excerpt is at most 8192 bytes")

    @classmethod
    def of(
        cls,
        *,
        url: str,
        session_id: str,
        capture_id: str,
        retrieved_at: datetime,
        status: int,
        content_type: str,
        body: bytes,
    ) -> "BrowserEvidence":
        """Hash and size come from the full body; the excerpt is its capped text."""
        return cls(
            url=url,
            session_id=session_id,
            capture_id=capture_id,
            retrieved_at=retrieved_at,
            status=status,
            content_type=content_type,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            excerpt=body[:MAX_EXCERPT_BYTES].decode("utf-8", "ignore"),
        )


@dataclass(frozen=True)
class BrowserRefusal:
    """A target that yielded no evidence, with a short reason code."""

    url: str
    reason: str

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.reason) is None:
            raise ValueError("reason must be a short code")


@dataclass(frozen=True)
class BrowserCapture:
    """The session's outcome: one evidence record or one refusal per target."""

    session_id: str
    evidence: tuple[BrowserEvidence, ...]
    refused: tuple[BrowserRefusal, ...]

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.session_id) is None:
            raise ValueError("a session id is a short opaque token")
        if any(item.session_id != self.session_id for item in self.evidence):
            raise ValueError("evidence belongs to its own session")


class BrowserEvidenceError(Exception):
    """The capture could not be performed at all. Same reason rules as
    ResearchProviderError: short code, provider text never echoed;
    `unavailable` means contact failed rather than a refusal."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class BrowserEvidenceProvider(Protocol):
    """Bounded page capture (ADR-017 decision 8). Absent by default; live
    providers need owner-approved credentials and an operator allowlist."""

    def describe(self) -> AiCapabilities: ...

    async def capture(self, request: BrowserCaptureRequest) -> BrowserCapture: ...
