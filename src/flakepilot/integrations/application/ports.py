"""Vendor-neutral port contracts owned by INT (ADR-003). Adapters live in infrastructure."""

from dataclasses import dataclass
from typing import Protocol


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
