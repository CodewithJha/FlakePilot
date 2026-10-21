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


class SecretStore(Protocol):
    """Write-only for domain and application layers: no operation returns a secret value
    (ADR-003 decision 3). Values are resolved only inside INT adapters."""

    async def put(self, scope: SecretScope, name: str, value: str) -> str: ...

    async def delete(self, scope: SecretScope, name: str) -> None: ...
