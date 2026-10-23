"""Operator grant administration: outcomes, validation, audit events, the session
revocation invariant, and compatibility with the real `decide()` rules (ADR-011, D2)."""

import asyncio
import re
import uuid
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

import pytest

from flakepilot.identity.application.administration import (
    InvalidAdminCommand,
    access_overview,
    add_member,
    grant_repo_access,
    overview_repos,
    revoke_repo_access,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Decision,
    Grant,
    OrgAccess,
    decide,
)
from flakepilot.identity.application.iam import (
    OPERATOR_CLI_ISSUER_ID,
    CiUploadPrincipal,
)
from flakepilot.shared_kernel.events import Channel, Event, Origin

CLI = Origin(Channel.CLI, "run-1")
ISSUER = "https://github.com"
ORG = str(uuid.uuid4())
OTHER_ORG = str(uuid.uuid4())
REPO = str(uuid.uuid4())
OTHER_REPO = str(uuid.uuid4())
NO_UUID = "not-a-uuid"
SUBJECT = "8001"
USER = "user-8001"


class FakeAdminStore:
    """In-memory mirror of the IAM surfaces these use cases touch; the membership
    foreign key is respected: a non-member grant raises like the database does."""

    def __init__(self) -> None:
        self.orgs: set[str] = set()
        self.users: dict[tuple[str, str], str] = {}
        self.memberships: dict[tuple[str, str], bool] = {}
        self.grants: dict[tuple[str, str, str, str], str] = {}
        self.live_sessions: dict[str, int] = {}
        self.events: list[Event] = []
        self.tokens: dict[bytes, tuple[str, str, str]] = {}
        self.sessions: dict[bytes, str] = {}

    async def append_event(self, event: Event) -> None:
        self.events.append(event)

    async def upsert_user(
        self, *, issuer: str, subject: str, login: str
    ) -> tuple[str, bool]:
        raise AssertionError("these use cases never upsert users")

    async def create_session(
        self, *, id_hash: bytes, user_id: str, lifetime: timedelta
    ) -> None:
        self.sessions[id_hash] = user_id

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> str | None:
        raise AssertionError("these use cases never revoke one session")

    async def active_session_user(
        self, *, id_hash: bytes, idle: timedelta, touch_after: timedelta
    ) -> str | None:
        return self.sessions.get(id_hash)

    async def create_ci_upload_token(
        self,
        *,
        token_id: str,
        org_id: str,
        repo_id: str,
        token_hash: bytes,
        display_prefix: str,
        created_by: str,
    ) -> None:
        self.tokens[token_hash] = (token_id, org_id, repo_id)

    async def revoke_ci_upload_token(
        self, *, org_id: str, repo_id: str, token_id: str
    ) -> bool:
        raise AssertionError("these use cases never revoke CI tokens")

    async def active_ci_upload_token(
        self, *, token_hash: bytes
    ) -> CiUploadPrincipal | None:
        token_id, org_id, repo_id = self.tokens[token_hash]
        return CiUploadPrincipal(token_id=token_id, org_id=org_id, repo_id=repo_id)

    async def create_org(self, *, org_id: str, name: str) -> bool:
        """False, writing nothing, if an org name equal ignoring case exists."""
        raise AssertionError("these use cases never create orgs")

    async def org_exists(self, *, org_id: str) -> bool:
        return org_id in self.orgs

    async def find_user(self, *, issuer: str, subject: str) -> str | None:
        return self.users.get((issuer, subject))

    async def ensure_user(
        self, *, issuer: str, subject: str, login: str | None
    ) -> tuple[str, bool]:
        user_id = f"user-{subject}"
        created = (issuer, subject) not in self.users
        if created:
            self.users[(issuer, subject)] = user_id
            self.live_sessions[user_id] = 0
        return user_id, created

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None:
        admin = self.memberships.get((org_id, user_id))
        if admin is None:
            return None
        grants = frozenset(
            (repo, Grant(grant))
            for (org, user, repo, grant) in self.grants
            if org == org_id and user == user_id
        )
        return OrgAccess(user_id, org_id, admin, grants)

    async def add_membership(
        self, *, org_id: str, user_id: str, org_admin: bool
    ) -> None:
        if user_id not in self.live_sessions:  # the store would defer to a FK instead
            raise AssertionError("ensure_user must come first")
        self.memberships[(org_id, user_id)] = org_admin

    async def add_grant(
        self,
        *,
        org_id: str,
        user_id: str,
        repo_id: str,
        grant: Grant,
        granted_by: str,
    ) -> bool:
        if (org_id, user_id) not in self.memberships:
            raise AssertionError("the (org, user) foreign key must have refused this")
        key = (org_id, user_id, repo_id, grant.value)
        if key in self.grants:
            return False
        self.grants[key] = granted_by
        return True

    async def revoke_grant(
        self, *, org_id: str, user_id: str, repo_id: str, grant: Grant
    ) -> bool:
        key = (org_id, user_id, repo_id, grant.value)
        existed = key in self.grants
        self.grants.pop(key, None)
        return existed

    async def revoke_user_sessions(self, *, user_id: str, reason: str) -> int:
        assert reason == "privilege_change"
        count = self.live_sessions.get(user_id, 0)
        self.live_sessions[user_id] = 0
        return count


def store() -> FakeAdminStore:
    result = FakeAdminStore()
    result.orgs.add(ORG)
    return result


def enrolled(
    subject: str, *, admin: bool = False, live_sessions: int = 2
) -> FakeAdminStore:
    fake = store()
    fake.users[(ISSUER, subject)] = USER
    fake.live_sessions[USER] = live_sessions
    fake.memberships[(ORG, USER)] = admin
    return fake


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def names(fake: FakeAdminStore) -> list[str]:
    return [event.name for event in fake.events]


def existing_user(*, live_sessions: int = 2) -> FakeAdminStore:
    """A user that signed in (or was bootstrapped) before: not yet a member."""
    fake = store()
    fake.users[(ISSUER, SUBJECT)] = USER
    fake.live_sessions[USER] = live_sessions
    return fake


def test_add_member_writes_membership_event_and_revokes_sessions() -> None:
    fake = existing_user()
    user_id, outcome = run(
        add_member(
            fake,
            org_id=ORG,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            member_login=None,
            org_admin=False,
            origin=CLI,
        )
    )
    assert outcome == "added" and user_id == USER
    assert fake.memberships == {(ORG, USER): False}
    assert names(fake) == ["iam.member.added", "iam.session.revoked"]
    added_event = fake.events[0]
    assert added_event.subject.type == "user" and added_event.subject.id == USER
    assert added_event.org_id == ORG
    assert added_event.payload == {"org_admin": False}
    revocation = fake.events[1]
    assert revocation.payload == {"sessions": 2}
    assert revocation.reason == "privilege_change"
    assert (added_event.actor.type.value, added_event.actor.id) == (
        "operator_cli",
        None,
    )


def test_add_member_revokes_nothing_for_a_user_without_sessions() -> None:
    fake = existing_user(live_sessions=0)
    _user_id, outcome = run(
        add_member(
            fake,
            org_id=ORG,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            member_login=None,
            org_admin=False,
            origin=CLI,
        )
    )
    assert outcome == "added"
    assert names(fake) == ["iam.member.added"]


def test_add_member_records_the_admin_flag_and_revokes() -> None:
    fake = existing_user()
    _outcome = run(
        add_member(
            fake,
            org_id=ORG,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            member_login="octo",
            org_admin=True,
            origin=CLI,
        )
    )
    assert fake.memberships == {(ORG, USER): True}
    assert fake.events[0].payload == {"org_admin": True}


def test_add_member_repeat_changes_nothing_and_appends_nothing() -> None:
    fake = enrolled(SUBJECT)
    _user_id, outcome = run(
        add_member(
            fake,
            org_id=ORG,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            member_login=None,
            org_admin=False,
            origin=CLI,
        )
    )
    assert outcome == "already_member"
    assert fake.events == []


def test_add_member_refuses_an_admin_flag_flip_writing_nothing() -> None:
    fake = enrolled(SUBJECT)
    with pytest.raises(InvalidAdminCommand):
        run(
            add_member(
                fake,
                org_id=ORG,
                member_issuer=ISSUER,
                member_subject=SUBJECT,
                member_login=None,
                org_admin=True,
                origin=CLI,
            )
        )
    assert fake.events == []
    assert fake.memberships == {(ORG, USER): False}


def test_add_member_refuses_an_unknown_org() -> None:
    fake = store()
    with pytest.raises(InvalidAdminCommand, match="no such org"):
        run(
            add_member(
                fake,
                org_id=OTHER_ORG,
                member_issuer=ISSUER,
                member_subject=SUBJECT,
                member_login=None,
                org_admin=False,
                origin=CLI,
            )
        )
    assert fake.events == [] and fake.users == {}


@pytest.mark.parametrize(
    ("match", "org_id", "subject", "login"),
    [
        ("--org-id", NO_UUID, SUBJECT, None),
        ("--member-github-id", ORG, "0", None),
        ("--member-github-id", ORG, "x", None),
        ("--member-login", ORG, SUBJECT, "bad login"),
    ],
)
def test_invalid_add_member_input_writes_nothing(
    match: str, org_id: str, subject: str, login: str | None
) -> None:
    fake = store()
    with pytest.raises(InvalidAdminCommand, match=re.escape(match)):
        run(
            add_member(
                fake,
                org_id=org_id,
                member_issuer=ISSUER,
                member_subject=subject,
                member_login=login,
                org_admin=False,
                origin=CLI,
            )
        )
    assert fake.events == [] and fake.users == {} and fake.memberships == {}


def grant_now(
    fake: FakeAdminStore, *, repo_id: str = REPO, grant: str = "repo.view"
) -> str:
    return run(
        grant_repo_access(
            fake,
            org_id=ORG,
            repo_id=repo_id,
            grant_value=grant,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            origin=CLI,
        )
    )


def test_grant_records_the_row_event_and_revokes_sessions() -> None:
    fake = enrolled(SUBJECT)
    assert grant_now(fake) == "granted"
    assert fake.grants == {(ORG, USER, REPO, "repo.view"): OPERATOR_CLI_ISSUER_ID}
    granted_event = fake.events[0]
    assert granted_event.name == "iam.grant.granted"
    assert granted_event.org_id == ORG and granted_event.repo_id == REPO
    assert granted_event.subject.id == USER
    assert granted_event.after == "held" and granted_event.before is None
    assert granted_event.payload == {"grant": "repo.view"}
    assert names(fake) == ["iam.grant.granted", "iam.session.revoked"]
    assert fake.live_sessions == {USER: 0}


def test_grant_repeat_is_already_held_without_events_or_revocation() -> None:
    fake = enrolled(SUBJECT)
    assert grant_now(fake) == "granted"
    assert grant_now(fake) == "already_held"
    assert names(fake) == ["iam.grant.granted", "iam.session.revoked"]


def test_grant_refuses_a_non_member() -> None:
    fake = store()
    fake.users[(ISSUER, SUBJECT)] = USER
    fake.live_sessions[USER] = 2
    with pytest.raises(InvalidAdminCommand, match="not a member"):
        grant_now(fake)
    assert fake.events == [] and fake.grants == {}


def test_grant_refuses_an_unknown_user() -> None:
    fake = store()
    with pytest.raises(InvalidAdminCommand, match="no such user"):
        grant_now(fake)
    assert fake.events == []


def test_grant_refuses_an_unknown_org() -> None:
    fake = store()
    with pytest.raises(InvalidAdminCommand, match="no such org"):
        run(
            grant_repo_access(
                fake,
                org_id=OTHER_ORG,
                repo_id=REPO,
                grant_value="repo.view",
                member_issuer=ISSUER,
                member_subject=SUBJECT,
                origin=CLI,
            )
        )
    assert fake.events == []


def test_grant_refuses_an_unsupported_permission() -> None:
    fake = enrolled(SUBJECT)
    with pytest.raises(InvalidAdminCommand, match="must be one of"):
        grant_now(fake, grant="repo.delete")
    assert fake.events == []


@pytest.mark.parametrize(
    ("grant", "repo_id"),
    [("repo.view", NO_UUID), ("repo.delete", NO_UUID)],
)
def test_invalid_grant_ids_are_refused(repo_id: str, grant: str) -> None:
    fake = enrolled(SUBJECT)
    with pytest.raises(InvalidAdminCommand, match="canonical lowercase UUID"):
        run(
            grant_repo_access(
                fake,
                org_id=ORG,
                repo_id=repo_id,
                grant_value=grant,
                member_issuer=ISSUER,
                member_subject=SUBJECT,
                origin=CLI,
            )
        )
    assert fake.events == []


def revoke_now(
    fake: FakeAdminStore, *, repo_id: str = REPO, grant: str = "repo.view"
) -> str:
    return run(
        revoke_repo_access(
            fake,
            org_id=ORG,
            repo_id=repo_id,
            grant_value=grant,
            member_issuer=ISSUER,
            member_subject=SUBJECT,
            origin=CLI,
        )
    )


def test_revoke_deletes_the_row_event_sessions() -> None:
    fake = enrolled(SUBJECT)
    assert grant_now(fake) == "granted"
    fake.events.clear()
    fake.live_sessions[USER] = 3
    assert revoke_now(fake) == "revoked"
    assert fake.grants == {}
    assert names(fake) == ["iam.grant.revoked", "iam.session.revoked"]
    revoked_event = fake.events[0]
    assert revoked_event.before == "held" and revoked_event.after == "revoked"
    assert revoked_event.payload == {"grant": "repo.view"}


def test_revoke_of_a_missing_grant_is_not_held_without_an_event() -> None:
    fake = enrolled(SUBJECT)
    assert revoke_now(fake) == "not_held"
    assert fake.events == []


def test_revoke_refuses_an_unknown_user() -> None:
    fake = store()
    with pytest.raises(InvalidAdminCommand, match="no such user"):
        revoke_now(fake)
    assert fake.events == []


def granted_access(*, admin: bool = False) -> OrgAccess:
    """The IAM record for a member holding repo.view on REPO, as `org_access` builds it."""
    return OrgAccess(
        USER,
        ORG,
        admin,
        frozenset({(REPO, Grant.VIEW)}),
    )


def test_grant_is_compatible_with_the_real_decide_rules() -> None:
    access = granted_access()
    assert (
        decide(
            access, Grant.VIEW, org_id=ORG, repo_id=REPO, code_host=CodeHostAccess.READ
        )
        is Decision.ALLOWED
    )
    assert (
        decide(
            access,
            Grant.INVESTIGATE,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
        )
        is Decision.FORBIDDEN
    )
    # A foreign org is NOT_FOUND, never "forbidden" (non-disclosure).
    assert (
        decide(
            access,
            Grant.VIEW,
            org_id=OTHER_ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
        )
        is Decision.NOT_FOUND
    )
    # A repo the member holds nothing on is NOT_FOUND.
    assert (
        decide(
            access,
            Grant.VIEW,
            org_id=ORG,
            repo_id=OTHER_REPO,
            code_host=CodeHostAccess.READ,
        )
        is Decision.NOT_FOUND
    )


def test_access_overview_walks_the_real_domain_rules() -> None:
    access = granted_access()
    overview = access_overview(access, [REPO, OTHER_REPO], CodeHostAccess.READ)
    assert overview.org_id == ORG and overview.org_admin is False
    repo, other = overview.repos
    assert repo.repo_id == REPO
    assert repo.explicit == frozenset({Grant.VIEW})
    assert repo.effective == frozenset({Grant.VIEW})
    assert repo.decisions == (
        (Grant.VIEW, "allowed"),
        (Grant.INVESTIGATE, "forbidden"),
        (Grant.APPROVE, "forbidden"),
        (Grant.CONFIGURE, "forbidden"),
    )
    assert other.explicit == frozenset() and other.effective == frozenset()
    assert all(decision == "not_found" for _grant, decision in other.decisions)


def test_access_overview_admin_implies_configure_without_approve() -> None:
    overview = access_overview(granted_access(admin=True), [REPO], CodeHostAccess.READ)
    repo = overview.repos[0]
    assert repo.explicit == frozenset({Grant.VIEW})
    assert Grant.CONFIGURE in repo.effective
    assert Grant.APPROVE not in repo.effective


def test_overview_repos_are_unique_and_sorted() -> None:
    access = OrgAccess(
        USER,
        ORG,
        False,
        frozenset(
            {
                ("z-repo", Grant.VIEW),
                ("a-repo", Grant.APPROVE),
                ("a-repo", Grant.VIEW),
            }
        ),
    )
    assert overview_repos(access) == ["a-repo", "z-repo"]
