"""IAM's `CodeHostAccessVerifier` port: the result separates evidence from absence."""

from flakepilot.identity.application.verification import AccessVerification
from flakepilot.identity.domain.access import CodeHostAccess


def test_verified_standings_carry_their_level() -> None:
    assert AccessVerification.NONE.standing is CodeHostAccess.NONE
    assert AccessVerification.READ.standing is CodeHostAccess.READ
    assert AccessVerification.WRITE.standing is CodeHostAccess.WRITE
    for answer in (
        AccessVerification.NONE,
        AccessVerification.READ,
        AccessVerification.WRITE,
    ):
        assert answer.is_verified


def test_every_unverified_outcome_denies_under_the_pure_rules() -> None:
    """Fail closed: unavailable, indeterminate and unbound are "cannot say", and
    "cannot say" maps to `CodeHostAccess.NONE`, never to a permission."""
    for answer in (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
    ):
        assert not answer.is_verified
        assert answer.standing is CodeHostAccess.NONE
        # The deny is the same standing a verified "no access" produces, so a
        # caller cannot accidentally grant on a missing answer.
        assert answer.standing is AccessVerification.NONE.standing


def test_the_standing_is_whatever_the_pure_decision_functions_accept() -> None:
    """The mapping target is IAM's existing domain type, so `decide`,
    `effective_grants` and `may_grant` consume verifier results unchanged."""
    from flakepilot.identity.domain.access import Grant, OrgAccess, effective_grants

    access = OrgAccess("u", "o", grants=frozenset({("r", Grant.VIEW)}))
    assert effective_grants(access, "r", AccessVerification.READ.standing) == frozenset(
        {Grant.VIEW}
    )
    assert (
        effective_grants(access, "r", AccessVerification.INDETERMINATE.standing)
        == frozenset()
    )
    assert (
        effective_grants(access, "r", AccessVerification.UNAVAILABLE.standing)
        == frozenset()
    )


def test_may_grant_consumes_verifier_outcomes_as_decision_i_requires() -> None:
    """The consumer contract for any future session-admin flow (ADR-011 I,
    ADR-015 §1): the flow calls `may_grant()` with the verifier's outcome.

    Every unverified outcome denies every grant — an outage, an ambiguous
    lookup, or a missing binding is never grant evidence. A verified read
    satisfies every grant except `repo.approve` (which needs write); a
    verified write satisfies all of them.
    """
    from flakepilot.identity.domain.access import Grant, OrgAccess, may_grant

    admin = OrgAccess("admin", "o", org_admin=True)
    unverified = (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
    )
    for outcome in unverified:
        assert all(
            not may_grant(
                admin,
                grant,
                org_id="o",
                repo_id="r",
                grantee_code_host=outcome.standing,
            )
            for grant in Grant
        ), outcome
    read = AccessVerification.READ.standing
    assert [
        may_grant(admin, g, org_id="o", repo_id="r", grantee_code_host=read)
        for g in (Grant.VIEW, Grant.INVESTIGATE, Grant.CONFIGURE)
    ] == [True, True, True]
    assert not may_grant(
        admin, Grant.APPROVE, org_id="o", repo_id="r", grantee_code_host=read
    )
    write = AccessVerification.WRITE.standing
    assert all(
        may_grant(admin, g, org_id="o", repo_id="r", grantee_code_host=write)
        for g in Grant
    )


def test_decide_keeps_a_wrongly_issued_grant_inert_under_unverified_outcomes() -> None:
    """ADR-015's reliance: an operator-issued grant row cannot activate through an
    unverified or denied code-host outcome — the intersection always denies."""
    from flakepilot.identity.domain.access import Decision, Grant, OrgAccess, decide

    access = OrgAccess("u", "o", grants=frozenset({("r", Grant.VIEW)}))
    for outcome in (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
        AccessVerification.NONE,
    ):
        assert (
            decide(
                access,
                Grant.VIEW,
                org_id="o",
                repo_id="r",
                code_host=outcome.standing,
            )
            is Decision.NOT_FOUND
        ), outcome
    assert (
        decide(
            access,
            Grant.VIEW,
            org_id="o",
            repo_id="r",
            code_host=AccessVerification.READ.standing,
        )
        is Decision.ALLOWED
    )
