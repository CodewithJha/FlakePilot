import pytest

from flakepilot.identity.application.authorization import (
    PermissionDenied,
    ResourceNotFound,
    authorize,
)
from flakepilot.identity.domain.access import CodeHostAccess, Grant, OrgAccess

VIEWER = OrgAccess("user-1", "org-1", grants=frozenset({("repo-1", Grant.VIEW)}))


def authorize_on(grant: Grant, org_id: str = "org-1") -> None:
    authorize(
        VIEWER, grant, org_id=org_id, repo_id="repo-1", code_host=CodeHostAccess.READ
    )


def test_allowed_returns_quietly() -> None:
    authorize_on(Grant.VIEW)


def test_visible_but_not_permitted_is_permission_denied() -> None:
    with pytest.raises(PermissionDenied):
        authorize_on(Grant.APPROVE)


def test_other_org_is_indistinguishable_from_missing() -> None:
    with pytest.raises(ResourceNotFound):
        authorize_on(Grant.VIEW, org_id="org-2")
