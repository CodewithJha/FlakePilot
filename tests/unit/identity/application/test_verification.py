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
