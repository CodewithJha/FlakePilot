"""Register a repository binding. The operator establishes which repos this deployment
serves; a binding is a routing fact, never evidence that any user has code-host access
(verified revision pinning still comes only from `pin_revision`, which stays
`source_unverified` until the code host confirms).

Outcomes and conflicts, in one unit of work:

- fresh `(repo_id, coordinates)` -> "registered" (one `repo.repository.registered`
  event, appended after the insert so a failed append rolls the insert back);
- exact repeat (same ID and same owner/name) -> "already_registered" (no event);
- the ID registered with different owner/name, or the coordinates registered under a
  different ID -> `RegistrationConflict`, nothing written;
- unusable inputs -> `SourceError`, nothing written.

`org_exists` is a synchronous IAM query the caller resolves (REPO may call IAM; IAM
cannot read REPO). The binding row has no foreign key by design, so the caller resolves
it BEFORE this function and never passes an org that does not exist.
"""

import uuid
from datetime import datetime
from typing import Protocol

from flakepilot.integrations.application.ports import (
    github_name_ok,
    github_owner_ok,
)
from flakepilot.repository.application.source import RepoBinding, SourceError
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

__all__ = [
    "OUTCOME_ALREADY_REGISTERED",
    "OUTCOME_REGISTERED",
    "RegistrationConflict",
    "RegistrationStore",
    "register_repository",
    "validate_binding",
]

OUTCOME_REGISTERED = "registered"
OUTCOME_ALREADY_REGISTERED = "already_registered"


class RegistrationConflict(Exception):
    """The repository ID or coordinates are taken by a different row. Nothing was
    written; the message names the conflict without echoing the input."""


class RegistrationStore(Protocol):
    async def register(self, binding: RepoBinding, *, now: datetime) -> str:
        """OUTCOME_REGISTERED or OUTCOME_ALREADY_REGISTERED; raises
        RegistrationConflict when the ID or the coordinates are taken by a
        different row."""

    async def append_event(self, event: Event) -> None: ...


def validate_binding(org_id: str, repo_id: str, owner: str, name: str) -> None:
    """Public entry command flows call before any database access, so unusable input is
    refused there. Machine reasons name the first offending field; the error text never
    echoes the input."""

    def _uuid(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except ValueError:
            return False

    if not _uuid(org_id):
        raise SourceError("invalid_org_id") from None
    if not _uuid(repo_id):
        raise SourceError("invalid_repo_id") from None
    if not github_owner_ok(owner):
        raise SourceError("invalid_owner") from None
    if not github_name_ok(name):
        raise SourceError("invalid_name") from None


async def register_repository(
    store: RegistrationStore,
    *,
    org_id: str,
    repo_id: str,
    owner: str,
    name: str,
    origin: Origin,
    now: datetime,
) -> str:
    """Register or re-confirm a GitHub repository binding for the org, in one unit of
    work. The org row must exist (resolved by the caller through IAM, synchronously).
    Raises `SourceError("invalid_repository")` for unusable inputs and
    `RegistrationConflict` when the ID or the coordinates are taken by a different
    row; nothing is written on either."""

    validate_binding(org_id, repo_id, owner, name)
    binding = RepoBinding(org_id=org_id, repo_id=repo_id, owner=owner, name=name)
    outcome = await store.register(binding, now=now)
    if outcome == OUTCOME_REGISTERED:
        await store.append_event(
            Event(
                "repo.repository.registered",
                Actor(ActorType.OPERATOR_CLI),
                origin,
                Subject("repository", repo_id),
                org_id=org_id,
                repo_id=repo_id,
                after="registered",
                payload={"owner": owner, "name": name},
            )
        )
    return outcome
