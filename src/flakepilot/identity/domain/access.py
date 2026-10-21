"""Repo access policy (PRODUCT-SCOPE §3.1, §3.3; D2). Pure rules, no I/O.

Effective access is the intersection of FlakePilot grants and code-host permission:
FlakePilot never gives more reach into a repository than the code host does.
"""

from dataclasses import dataclass
from enum import Enum, IntEnum, StrEnum


class Grant(StrEnum):
    VIEW = "repo.view"
    INVESTIGATE = "repo.investigate"
    APPROVE = "repo.approve"
    CONFIGURE = "repo.configure"


_IMPLIES = {
    Grant.VIEW: {Grant.VIEW},
    Grant.INVESTIGATE: {Grant.INVESTIGATE, Grant.VIEW},
    Grant.APPROVE: {Grant.APPROVE, Grant.VIEW},
    Grant.CONFIGURE: {Grant.CONFIGURE, Grant.INVESTIGATE, Grant.VIEW},
}


class CodeHostAccess(IntEnum):
    """The principal's permission on the repository at the code host (supplied by REPO)."""

    NONE = 0
    READ = 1
    WRITE = 2


@dataclass(frozen=True)
class OrgAccess:
    """A verified principal's standing in one org.

    Built only by IAM from its own records after authentication; never from request input.
    `grants` holds explicitly granted (repo_id, grant) pairs.
    """

    principal_id: str
    org_id: str
    org_admin: bool = False
    grants: frozenset[tuple[str, Grant]] = frozenset()


class Decision(Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"  # the repo is visible, the action is not
    NOT_FOUND = (
        "not_found"  # not visible: outside the org, no grant, or no code-host read
    )


def effective_grants(
    access: OrgAccess, repo_id: str, code_host: CodeHostAccess
) -> frozenset[Grant]:
    """Grants held on the repo, with implications. Org admins implicitly hold `configure`
    on every org repo but never `approve`. Nothing without code-host read access."""
    if code_host < CodeHostAccess.READ:
        return frozenset()
    held = {grant for repo, grant in access.grants if repo == repo_id}
    if access.org_admin:
        held.add(Grant.CONFIGURE)
    return frozenset(implied for grant in held for implied in _IMPLIES[grant])


def decide(
    access: OrgAccess,
    grant: Grant,
    *,
    org_id: str,
    repo_id: str,
    code_host: CodeHostAccess,
) -> Decision:
    """Deny by default. Anything the principal cannot view is NOT_FOUND, so a denial never
    reveals whether a repo exists in another org."""
    if access.org_id != org_id:
        return Decision.NOT_FOUND
    granted = effective_grants(access, repo_id, code_host)
    if Grant.VIEW not in granted:
        return Decision.NOT_FOUND
    if grant not in granted:
        return Decision.FORBIDDEN
    return Decision.ALLOWED


def may_grant(
    granter: OrgAccess,
    grant: Grant,
    *,
    org_id: str,
    repo_id: str,
    grantee_code_host: CodeHostAccess,
) -> bool:
    """Whether `granter` may give `grant` on the repo to a grantee.

    Granter: org admins any grant; `repo.configure` holders any grant on that repo except
    `repo.approve` (D2: only org admins grant `repo.approve`).
    Grantee: must already have matching code-host access (ADR-011 I): write for
    `repo.approve`, read for every other grant.
    """
    required = CodeHostAccess.WRITE if grant is Grant.APPROVE else CodeHostAccess.READ
    if granter.org_id != org_id or grantee_code_host < required:
        return False
    if granter.org_admin:
        return True
    explicitly = {g for repo, g in granter.grants if repo == repo_id}
    return grant is not Grant.APPROVE and Grant.CONFIGURE in explicitly
