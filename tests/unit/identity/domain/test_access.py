import pytest

from flakepilot.identity.domain.access import (
    CodeHostAccess,
    Decision,
    Grant,
    OrgAccess,
    decide,
    may_grant,
)

ORG, OTHER_ORG, REPO, OTHER_REPO = "org-1", "org-2", "repo-1", "repo-2"
READ = CodeHostAccess.READ


def member(*grants: Grant, org_admin: bool = False) -> OrgAccess:
    return OrgAccess("user-1", ORG, org_admin, frozenset((REPO, g) for g in grants))


def check(
    access: OrgAccess,
    grant: Grant,
    *,
    org_id: str = ORG,
    repo_id: str = REPO,
    code_host: CodeHostAccess = READ,
) -> Decision:
    return decide(access, grant, org_id=org_id, repo_id=repo_id, code_host=code_host)


@pytest.mark.parametrize("grant", list(Grant))
def test_members_have_no_repo_access_until_granted(grant: Grant) -> None:
    assert check(member(), grant) is Decision.NOT_FOUND


@pytest.mark.parametrize(
    ("held", "allowed"),
    [
        (Grant.VIEW, {Grant.VIEW}),
        (Grant.INVESTIGATE, {Grant.INVESTIGATE, Grant.VIEW}),
        (Grant.APPROVE, {Grant.APPROVE, Grant.VIEW}),
        (Grant.CONFIGURE, {Grant.CONFIGURE, Grant.INVESTIGATE, Grant.VIEW}),
    ],
)
def test_each_grant_allows_exactly_what_it_implies(
    held: Grant, allowed: set[Grant]
) -> None:
    for grant in Grant:
        expected = Decision.ALLOWED if grant in allowed else Decision.FORBIDDEN
        assert check(member(held), grant) is expected, (held, grant)


def test_org_admins_implicitly_configure_but_never_approve() -> None:
    admin = member(org_admin=True)
    assert check(admin, Grant.CONFIGURE) is Decision.ALLOWED
    assert check(admin, Grant.INVESTIGATE) is Decision.ALLOWED
    assert check(admin, Grant.APPROVE) is Decision.FORBIDDEN
    assert (
        check(member(Grant.APPROVE, org_admin=True), Grant.APPROVE) is Decision.ALLOWED
    )


@pytest.mark.parametrize("grant", list(Grant))
def test_other_orgs_are_not_found_even_for_org_admins(grant: Grant) -> None:
    admin = member(*Grant, org_admin=True)
    assert check(admin, grant, org_id=OTHER_ORG) is Decision.NOT_FOUND


def test_grants_do_not_carry_over_to_other_repos() -> None:
    assert check(member(*Grant), Grant.VIEW, repo_id=OTHER_REPO) is Decision.NOT_FOUND


def test_no_code_host_read_means_not_found_whatever_the_grants() -> None:
    admin = member(*Grant, org_admin=True)
    for grant in Grant:
        assert check(admin, grant, code_host=CodeHostAccess.NONE) is Decision.NOT_FOUND


def can_grant(
    granter: OrgAccess,
    grant: Grant,
    *,
    org_id: str = ORG,
    repo_id: str = REPO,
    grantee: CodeHostAccess = CodeHostAccess.WRITE,
) -> bool:
    return may_grant(
        granter, grant, org_id=org_id, repo_id=repo_id, grantee_code_host=grantee
    )


def test_only_org_admins_grant_approve() -> None:
    admin = member(org_admin=True)
    configurer = member(Grant.CONFIGURE)
    assert can_grant(admin, Grant.APPROVE)
    assert not can_grant(configurer, Grant.APPROVE)
    for grant in (Grant.VIEW, Grant.INVESTIGATE, Grant.CONFIGURE):
        assert can_grant(configurer, grant)
        assert not can_grant(configurer, grant, repo_id=OTHER_REPO)
        assert not can_grant(member(Grant.APPROVE), grant)
    assert not can_grant(admin, Grant.VIEW, org_id=OTHER_ORG)


@pytest.mark.parametrize("grant", list(Grant))
@pytest.mark.parametrize("grantee", list(CodeHostAccess))
def test_grantee_needs_matching_code_host_access(
    grant: Grant, grantee: CodeHostAccess
) -> None:
    required = CodeHostAccess.WRITE if grant is Grant.APPROVE else CodeHostAccess.READ
    admin = member(org_admin=True)
    assert can_grant(admin, grant, grantee=grantee) == (grantee >= required)
