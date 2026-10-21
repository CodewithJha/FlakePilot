"""Pin a repository revision. A caller-supplied SHA is not evidence by itself."""

import uuid
from dataclasses import dataclass
from typing import Protocol

from flakepilot.integrations.application.ports import (
    CodeHost,
    CodeHostError,
    commit_sha_ok,
)


class SourceError(Exception):
    """`reason` is a short code. `unavailable` means the host gave no answer."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        self.reason = reason
        self.unavailable = unavailable
        super().__init__(reason)


@dataclass(frozen=True)
class RepoBinding:
    """A repository this deployment has registered. Host is always GitHub."""

    org_id: str
    repo_id: str
    owner: str
    name: str


@dataclass(frozen=True)
class PinnedRevision:
    """`sha` is the value the code host returned, equal to the claimed SHA."""

    org_id: str
    repo_id: str
    owner: str
    name: str
    sha: str


class RepoDirectory(Protocol):
    async def get(self, repo_id: str) -> RepoBinding | None: ...


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


async def pin_revision(
    directory: RepoDirectory,
    host: CodeHost,
    *,
    org_id: str,
    repo_id: str,
    claimed_sha: str,
) -> PinnedRevision:
    """Confirm `claimed_sha` with the code host for a registered repository.

    Raises `SourceError`. `not_found` hides missing and cross-org ids.
    `source_unverified` means the host was not asked or did not answer.
    """
    if not (_uuid(org_id) and _uuid(repo_id) and commit_sha_ok(claimed_sha)):
        raise SourceError("invalid_revision")
    binding = await directory.get(repo_id)
    if binding is None or binding.org_id != org_id:
        raise SourceError("not_found")
    try:
        confirmed = await host.confirm_commit(
            owner=binding.owner, name=binding.name, claimed_sha=claimed_sha
        )
    except CodeHostError as exc:
        raise SourceError(exc.reason, unavailable=exc.unavailable) from None
    if confirmed != claimed_sha:
        raise SourceError("unverified_revision")
    return PinnedRevision(
        org_id=org_id,
        repo_id=repo_id,
        owner=binding.owner,
        name=binding.name,
        sha=confirmed,
    )
