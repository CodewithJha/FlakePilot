"""IAM's authorization decision point (SYSTEM-ARCHITECTURE §7.2).

Every application use case calls `authorize` before doing work. `OrgAccess` must come
from verified authentication and IAM's records; nothing builds it from request input.
Authentication itself is not implemented yet (see the Stage 2.3 step 3.7 report).
"""

from flakepilot.identity.domain.access import (
    CodeHostAccess,
    Decision,
    Grant,
    OrgAccess,
    decide,
)


class ResourceNotFound(Exception):
    """The resource is not visible to the principal (or does not exist)."""


class PermissionDenied(Exception):
    """The resource is visible, but the principal may not perform the action."""


def authorize(
    access: OrgAccess,
    grant: Grant,
    *,
    org_id: str,
    repo_id: str,
    code_host: CodeHostAccess,
) -> None:
    decision = decide(
        access, grant, org_id=org_id, repo_id=repo_id, code_host=code_host
    )
    if decision is Decision.NOT_FOUND:
        raise ResourceNotFound
    if decision is not Decision.ALLOWED:
        raise PermissionDenied
