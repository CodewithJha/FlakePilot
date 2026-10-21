"""Vendor-neutral port contracts owned by INT (ADR-003). Adapters live in infrastructure."""

import re
from dataclasses import dataclass
from typing import Protocol

_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class VerifiedIdentity:
    """A subject verified by the identity provider. Authentication only: never authorization."""

    issuer: str
    subject: str
    login: str


class IdentityProviderError(Exception):
    """Sign-in could not be verified. `reason` is a short, non-sensitive code for logs.

    `unavailable` means the provider could not answer properly (network, timeout, 5xx,
    unreadable response); otherwise it rejected the sign-in (bad code, invalid identity).
    """

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class IdentityProvider(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity: ...


@dataclass(frozen=True)
class SecretScope:
    """Instance (no IDs), org, or repo (needs its org) scope of a secret."""

    org_id: str | None = None
    repo_id: str | None = None

    def __post_init__(self) -> None:
        if self.repo_id is not None and self.org_id is None:
            raise ValueError("a repo-scoped secret needs its org")


@dataclass(frozen=True)
class ArtifactRef:
    """A stored object: its key, SHA-256 (hex) and size. Keys are content-addressed."""

    key: str
    sha256: str
    size: int


class ArtifactStoreError(Exception):
    """`kind`: `not_found`, `integrity` (content does not match its hash) or
    `invalid_request`. Never carries content or paths."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ArtifactStore(Protocol):
    """Immutable blob storage (SYSTEM-ARCHITECTURE §4). Objects are never overwritten;
    the hash is checked on every read; keys are namespaced by owner, org and repo."""

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef: ...

    async def get(self, ref: ArtifactRef) -> bytes: ...


@dataclass(frozen=True)
class ParsedTestCase:
    """One test case from a report. `attempts` are outcomes in order: `passed`,
    `failed`, `errored` or `skipped`."""

    classname: str
    name: str
    attempts: tuple[str, ...]


class ReportRejected(Exception):
    """The report is refused. `reason` is a short machine-readable code; the report's
    content is never included."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReportParser(Protocol):
    """CI/report port (SYSTEM-ARCHITECTURE §4): deterministic, bounded parsing of
    untrusted bytes. Same bytes, same result."""

    def parse(self, data: bytes) -> list[ParsedTestCase]: ...


class SecretStore(Protocol):
    """Write-only for domain and application layers: no operation returns a secret value
    (ADR-003 decision 3). Values are resolved only inside INT adapters."""

    async def put(self, scope: SecretScope, name: str, value: str) -> str: ...

    async def delete(self, scope: SecretScope, name: str) -> None: ...


def _code(value: str, label: str) -> None:
    if _CODE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a short code")


@dataclass(frozen=True)
class AiCapabilities:
    """What an assist adapter can do. `available` is false when no model is wired."""

    available: bool
    provider: str

    def __post_init__(self) -> None:
        _code(self.provider, "provider")


@dataclass(frozen=True)
class AiDataPart:
    """One untrusted input. The adapter must not treat it as instructions."""

    name: str
    text: str

    def __post_init__(self) -> None:
        _code(self.name, "data part name")


@dataclass(frozen=True)
class AiRequest:
    """`instructions` are trusted. `data_parts` are untrusted evidence text."""

    instructions: str
    data_parts: tuple[AiDataPart, ...]
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True)
class AiResponse:
    """`available` carries `output` and no reason. Otherwise `output` is absent and
    `reason` is a short code. Model text never rides in `reason`."""

    available: bool
    output: str | None
    reason: str | None
    provider: str
    model: str | None

    def __post_init__(self) -> None:
        _code(self.provider, "provider")
        if self.model is not None:
            _code(self.model, "model")
        if self.available:
            if not isinstance(self.output, str) or self.reason is not None:
                raise ValueError("an available response has output and no reason")
        elif self.output is not None or self.reason is None:
            raise ValueError("an unavailable response has a reason and no output")
        elif _CODE.fullmatch(self.reason) is None:
            raise ValueError("reason must be a short code")


class AiAssistPort(Protocol):
    """Assist port (ADR-009). No tools and no side effects. Adapters do not authorize."""

    def describe(self) -> AiCapabilities: ...

    async def submit(self, request: AiRequest) -> AiResponse: ...
