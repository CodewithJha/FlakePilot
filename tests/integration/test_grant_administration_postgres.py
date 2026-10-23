"""Grant administration against real PostgreSQL: registration conflicts, grant round
trips, `decide()` compatibility over the real store, the session-revocation invariant,
rollback on a failed audit append, CLI refusals without cross-org disclosure, and the
relay publishing the `repo` outbox."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.identity.application.administration import add_member
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Decision,
    Grant,
    decide,
)
from flakepilot.identity.application.iam import Iam, IamStore, SessionPrincipal, digest
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.application.ports import VerifiedIdentity
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.repository.application.registration import (
    RegistrationConflict,
    register_repository,
)
from flakepilot.repository.infrastructure.registry import SqlRegistrationStore
from flakepilot.shared_kernel.events import Channel, Origin

pytestmark = pytest.mark.integration

ISSUER = "https://github.com"
ADMIN_ID, MEMBER_ID, OTHER_ID = "7007", "8001", "8002"


def origin() -> Origin:
    return Origin(Channel.CLI, str(uuid.uuid4()))


class _IdentifyFake:
    """The sign-in fake: one verified GitHub identity per instance."""

    def __init__(self, subject: str) -> None:
        self.subject = subject

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://github.test/authorize?state={state}"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        return VerifiedIdentity(ISSUER, self.subject, f"member-{self.subject}")


def _cli(url: str, command: str, *argv: str) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    return subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root", command, *argv],
        env={**environ, "FLAKEPILOT_DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def sql(engine: AsyncEngine, statement: str, **params: object) -> list[Any]:
    async with transaction(engine) as session:
        result = await session.execute(text(statement), params)
        returns_rows = isinstance(result, CursorResult) and result.returns_rows
        return list(result) if returns_rows else []


def iam(engine: AsyncEngine, provider: object = None) -> Iam:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[IamStore]:
        async with transaction(engine) as session:
            yield SqlIamStore(session)

    return Iam(unit_of_work, provider)  # type: ignore[arg-type]


async def sign_in(engine: AsyncEngine, subject: str = MEMBER_ID) -> str:
    service = iam(engine, _IdentifyFake(subject))
    return await service.complete_sign_in(
        expected_state="s",
        received_state="s",
        code="c",
        code_verifier="v",
        error=None,
        previous_session=None,
        origin=origin(),
    )


async def outbox_rows(
    engine: AsyncEngine, where: str, table: str = "iam", **params: object
) -> list[Any]:
    return await sql(
        engine,
        "SELECT name, org_id::text, repo_id::text, actor_type, actor_id::text, channel, "
        "correlation_id, subject_type, subject_id::text, before, after, reason, "
        f"payload::text, published_at FROM {table}.outbox WHERE {where} ORDER BY seq",
        **params,
    )


def assert_no_secrets(rows: list[Any], *secrets: str | bytes) -> None:
    shown_rows = repr([tuple(row) for row in rows])
    for secret in secrets:
        shown = secret.hex() if isinstance(secret, bytes) else secret
        assert shown not in shown_rows


async def bootstrapped_org(engine: AsyncEngine, name: str) -> str:
    return await iam(engine).bootstrap_org(
        name=name,
        admin_issuer=ISSUER,
        admin_subject=ADMIN_ID,
        admin_login=None,
        origin=origin(),
    )


async def register(
    engine: AsyncEngine,
    *,
    org_id: str,
    repo_id: str,
    owner: str,
    name: str,
    store: type[SqlRegistrationStore] | None = None,
) -> str:
    async with transaction(engine) as session:
        return await register_repository(
            (store or SqlRegistrationStore)(session),
            org_id=org_id,
            repo_id=repo_id,
            owner=owner,
            name=name,
            origin=origin(),
            now=datetime.now(UTC),
        )


async def add_member_now(engine: AsyncEngine, org_id: str) -> str:
    async with transaction(engine) as session:
        _user_id, outcome = await add_member(
            SqlIamStore(session),
            org_id=org_id,
            member_issuer=ISSUER,
            member_subject=MEMBER_ID,
            member_login="member-8001",
            org_admin=False,
            origin=origin(),
        )
    return outcome


def suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    """A module-shared migrated database, like the other IAM integration modules."""
    return migrated_database_url


def test_register_repository_round_trip_conflicts_and_relay(url: str) -> None:
    repo_id = str(uuid.uuid4())
    owner = f"owner-{suffix()}"

    async def scenario(engine: AsyncEngine) -> tuple[str, list[Any]]:
        org_id = await bootstrapped_org(engine, f"Registration {suffix()}")
        outcome = await register(
            engine, org_id=org_id, repo_id=repo_id, owner=owner, name="acme"
        )
        repeat = await register(
            engine, org_id=org_id, repo_id=repo_id, owner=owner, name="acme"
        )
        assert outcome == "registered"
        assert repeat == "already_registered"
        rows = await outbox_rows(
            engine, "name = 'repo.repository.registered'", table="repo"
        )
        return org_id, rows

    org_id, rows = run(url, scenario)
    assert [
        (
            row.name,
            row.org_id,
            row.repo_id,
            row.actor_type,
            row.actor_id,
            row.subject_type,
            row.after,
            row.payload,
        )
        for row in rows
    ] == [
        (
            "repo.repository.registered",
            org_id,
            repo_id,
            "operator_cli",
            None,
            "repository",
            "registered",
            json.dumps({"name": "acme", "owner": owner}),
        )
    ]

    async def conflicts(engine: AsyncEngine) -> str:
        rival = await bootstrapped_org(engine, f"Rival {suffix()}")
        with pytest.raises(RegistrationConflict):
            await register(
                engine, org_id=org_id, repo_id=repo_id, owner=owner, name="other"
            )
        with pytest.raises(RegistrationConflict):
            await register(
                engine,
                org_id=org_id,
                repo_id=str(uuid.uuid4()),
                owner=owner,
                name="acme",
            )
        # Coordinates are global: another org cannot claim the same repository.
        with pytest.raises(RegistrationConflict):
            await register(
                engine,
                org_id=rival,
                repo_id=str(uuid.uuid4()),
                owner=owner,
                name="acme",
            )
        [[stored]] = await sql(
            engine,
            "SELECT org_id::text FROM repo.repositories WHERE id = :r",
            r=repo_id,
        )
        return str(stored)

    assert run(url, conflicts) == org_id
    [[event_count]] = run(
        url,
        lambda e: sql(
            e,
            "SELECT count(*) FROM repo.outbox WHERE name = 'repo.repository.registered'",
        ),
    )
    assert event_count == 1  # conflicts wrote nothing and appended nothing

    run(url, lambda e: _relay_and_assert(e, repo_id))


async def _relay_and_assert(engine: AsyncEngine, repo_id: str) -> None:
    from flakepilot.interface.composition_root.relay import deliver_batch

    published = await deliver_batch(engine, 100)
    assert published >= 1
    rows = await sql(
        engine,
        "SELECT source, name, repo_id::text, recorded_at FROM aud.events "
        "WHERE source = 'repo' AND name = 'repo.repository.registered'",
    )
    assert [(r[0], r[1], r[2], r[3] is not None) for r in rows] == [
        ("repo", "repo.repository.registered", repo_id, True)
    ]


def test_a_failed_registration_event_rolls_the_insert_back(url: str) -> None:
    class FailingRegistration(SqlRegistrationStore):
        """Appends every event twice: the second insert violates the primary key."""

        async def append_event(self, event: Any) -> None:
            await super().append_event(event)
            await super().append_event(event)

    async def scenario(engine: AsyncEngine) -> tuple[int, int, str]:
        org_id = await bootstrapped_org(engine, f"Rollback {suffix()}")
        correlation_id = str(uuid.uuid4())
        try:
            async with transaction(engine) as session:
                await register_repository(
                    FailingRegistration(session),
                    org_id=org_id,
                    repo_id=str(uuid.uuid4()),
                    owner=f"owner-{suffix()}",
                    name="acme",
                    origin=Origin(Channel.CLI, correlation_id),
                    now=datetime.now(UTC),
                )
        except exc.IntegrityError:
            pass
        else:
            raise AssertionError("the failing outbox must fail the whole run")
        [[bindings]] = await sql(
            engine, "SELECT count(*) FROM repo.repositories WHERE org_id = :o", o=org_id
        )
        [[events]] = await sql(
            engine,
            "SELECT count(*) FROM repo.outbox WHERE correlation_id = :c",
            c=correlation_id,
        )
        return int(bindings), int(events), org_id

    bindings, events, _org = run(url, scenario)
    assert (bindings, events) == (0, 0)


def test_member_and_grant_round_trip_with_decide_compatibility(url: str) -> None:
    repo_id = str(uuid.uuid4())
    owner = f"owner-{suffix()}"

    async def scenario(engine: AsyncEngine) -> tuple[str, str]:
        org_id = await bootstrapped_org(engine, f"Round trip {suffix()}")
        await register(engine, org_id=org_id, repo_id=repo_id, owner=owner, name="acme")
        return org_id, repo_id

    org_id, _repo = run(url, scenario)

    added = _cli(
        url,
        "add-member",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--member-login",
        f"member-{MEMBER_ID}",
    )
    assert added.returncode == 0 and added.stdout == "added\n", added.stderr
    again = _cli(url, "add-member", "--org-id", org_id, "--member-github-id", MEMBER_ID)
    assert again.returncode == 0 and again.stdout == "already_member\n", again.stderr

    granted = _cli(
        url,
        "grant",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert granted.returncode == 0 and granted.stdout == "granted\n", granted.stderr
    held = _cli(
        url,
        "grant",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert held.returncode == 0 and held.stdout == "already_held\n", held.stderr

    async def decis(
        engine: AsyncEngine, org: str
    ) -> tuple[Decision, Decision, Decision, Decision]:
        [[member_user]] = await sql(
            engine,
            "SELECT id::text FROM iam.users WHERE idp_subject = :s",
            s=MEMBER_ID,
        )
        async with transaction(engine) as session:
            access = await SqlIamStore(session).org_access(
                user_id=member_user, org_id=org
            )
        assert access is not None
        read = CodeHostAccess.READ
        return (
            decide(access, Grant.VIEW, org_id=org, repo_id=repo_id, code_host=read),
            decide(
                access, Grant.INVESTIGATE, org_id=org, repo_id=repo_id, code_host=read
            ),
            decide(
                access,
                Grant.VIEW,
                org_id=str(uuid.uuid4()),
                repo_id=repo_id,
                code_host=read,
            ),
            decide(
                access,
                Grant.VIEW,
                org_id=org,
                repo_id=str(uuid.uuid4()),
                code_host=read,
            ),
        )

    view, investigate, foreign_org, other_repo = run(url, lambda e: decis(e, org_id))
    assert view is Decision.ALLOWED
    assert investigate is Decision.FORBIDDEN
    assert foreign_org is Decision.NOT_FOUND  # a foreign org is not distinguishable
    assert other_repo is Decision.NOT_FOUND  # no grant, no view: non-disclosure

    revoked = _cli(
        url,
        "revoke",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert revoked.returncode == 0 and revoked.stdout == "revoked\n"
    repeat = _cli(
        url,
        "revoke",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert repeat.returncode == 0 and repeat.stdout == "not_held\n"

    async def after_revoke(engine: AsyncEngine) -> tuple[Decision | None, list[Any]]:
        view_after = await _decide_for(engine, org_id, repo_id)
        events = await outbox_rows(
            engine,
            "org_id = :o AND subject_id IN (SELECT id FROM iam.users "
            "WHERE idp_subject IN (:a, :m))",
            o=org_id,
            a=ADMIN_ID,
            m=MEMBER_ID,
        )
        return view_after, events

    view_after, events = run(url, after_revoke)
    assert view_after is Decision.NOT_FOUND
    names = [row.name for row in events if row.name.startswith("iam.grant")]
    assert names == ["iam.grant.granted", "iam.grant.revoked"]
    grant_events = [row for row in events if row.name.startswith("iam.grant")]
    assert {row.channel for row in grant_events} == {"cli"}
    assert {row.actor_type for row in grant_events} == {"operator_cli"}
    assert {json.loads(row.payload)["grant"] for row in grant_events} == {"repo.view"}


async def _member_user(engine: AsyncEngine) -> str:
    [[user_id]] = await sql(
        engine, "SELECT id::text FROM iam.users WHERE idp_subject = :s", s=MEMBER_ID
    )
    return str(user_id)


async def _decide_for(
    engine: AsyncEngine, org_id: str, repo_id: str
) -> Decision | None:
    [[member_user]] = await sql(
        engine, "SELECT id::text FROM iam.users WHERE idp_subject = :s", s=MEMBER_ID
    )
    async with transaction(engine) as session:
        access = await SqlIamStore(session).org_access(
            user_id=member_user, org_id=org_id
        )
    if access is None:
        return None
    return decide(
        access,
        Grant.VIEW,
        org_id=org_id,
        repo_id=repo_id,
        code_host=CodeHostAccess.READ,
    )


def test_membership_and_grant_changes_revoke_live_sessions(url: str) -> None:
    repo_id = str(uuid.uuid4())
    owner = f"owner-{suffix()}"

    async def scenario(engine: AsyncEngine) -> tuple[str, str]:
        org_id = await bootstrapped_org(engine, f"Sessions {suffix()}")
        await register(engine, org_id=org_id, repo_id=repo_id, owner=owner, name="acme")
        session = await sign_in(engine)  # member exists with a live session
        service = iam(engine, _IdentifyFake(MEMBER_ID))
        assert await service.authenticate(session) == SessionPrincipal(
            await _member_user(engine)
        )
        return org_id, session

    org_id, first_session = run(url, scenario)

    async def joined(engine: AsyncEngine) -> tuple[SessionPrincipal | None, list[Any]]:
        assert await add_member_now(engine, org_id) == "added"
        service = iam(engine, _IdentifyFake(MEMBER_ID))
        principal_after = await service.authenticate(first_session)
        rows = await sql(
            engine,
            "SELECT revoke_reason FROM iam.sessions WHERE id_hash = :h",
            h=digest(first_session),
        )
        return principal_after, [tuple(row) for row in rows]

    principal_after, reasons = run(url, joined)
    assert principal_after is None
    assert reasons == [("privilege_change",)]

    second_session = run(url, lambda e: sign_in(e))
    granted = _cli(
        url,
        "grant",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert granted.returncode == 0, granted.stderr

    async def after_grant(
        engine: AsyncEngine,
    ) -> tuple[SessionPrincipal | None, list[Any]]:
        service = iam(engine, _IdentifyFake(MEMBER_ID))
        principal_after_grant = await service.authenticate(second_session)
        events = await outbox_rows(
            engine, "subject_id = :u", u=await _member_user(engine)
        )
        return principal_after_grant, events

    principal_after_grant, events = run(url, after_grant)
    assert principal_after_grant is None
    revocations = [row for row in events if row.name == "iam.session.revoked"]
    assert {
        (row.reason, json.loads(row.payload)["sessions"]) for row in revocations
    } >= {("privilege_change", 1)}
    assert all(row.subject_type == "user" for row in revocations)
    # No session IDs or hashes in events.
    assert_no_secrets(events, first_session, second_session)


def test_cli_refuses_foreign_and_unregistered_repos_without_disclosure(
    url: str,
) -> None:
    repo_id = str(uuid.uuid4())
    owner = f"owner-{suffix()}"

    async def scenario(engine: AsyncEngine) -> tuple[str, str]:
        org_a = await bootstrapped_org(engine, f"Org A {suffix()}")
        org_b = await bootstrapped_org(engine, f"Org B {suffix()}")
        await register(engine, org_id=org_b, repo_id=repo_id, owner=owner, name="acme")
        return org_a, org_b

    org_a, org_b = run(url, scenario)
    joined = _cli(url, "add-member", "--org-id", org_a, "--member-github-id", MEMBER_ID)
    assert joined.returncode == 0, joined.stderr

    foreign = _cli(
        url,
        "grant",
        "--org-id",
        org_a,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    # Registered under org B: refused, without naming org B.
    assert foreign.returncode == 1, foreign.stdout
    assert foreign.stderr == "grant: the repository is not registered in this org\n"
    assert org_b not in foreign.stdout + foreign.stderr

    unregistered = _cli(
        url,
        "grant",
        "--org-id",
        org_a,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        str(uuid.uuid4()),
        "--grant",
        "repo.view",
    )
    # Unregistered: the same single message as foreign-org, by design.
    assert (
        unregistered.stderr == "grant: the repository is not registered in this org\n"
    )
    assert unregistered.stdout == ""

    repeat_revoke = _cli(
        url,
        "revoke",
        "--org-id",
        org_a,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        str(uuid.uuid4()),
        "--grant",
        "repo.view",
    )
    assert repeat_revoke.returncode == 0 and repeat_revoke.stdout == "not_held\n"


def test_show_access_reports_decisions_and_non_disclosure(url: str) -> None:
    repo_id = str(uuid.uuid4())
    owner = f"owner-{suffix()}"

    async def scenario(engine: AsyncEngine) -> str:
        org = await bootstrapped_org(engine, f"Show access {suffix()}")
        await register(engine, org_id=org, repo_id=repo_id, owner=owner, name="acme")
        assert await add_member_now(engine, org) == "added"
        return org

    org_id = run(url, scenario)
    granted = _cli(
        url,
        "grant",
        "--org-id",
        org_id,
        "--member-github-id",
        MEMBER_ID,
        "--repo-id",
        repo_id,
        "--grant",
        "repo.view",
    )
    assert granted.returncode == 0, granted.stderr
    shown = _cli(
        url, "show-access", "--org-id", org_id, "--member-github-id", MEMBER_ID
    )
    assert shown.returncode == 0, shown.stderr
    text_out = shown.stdout
    assert text_out.startswith(f"org {org_id} (org_admin=false)")
    assert repo_id in text_out
    assert "repo.view=allowed" in text_out and "repo.investigate=forbidden" in text_out
    assert "not verified" in text_out  # the standing is never presented as verified

    unknown_member = _cli(
        url, "show-access", "--org-id", org_id, "--member-github-id", "99999999"
    )
    assert unknown_member.returncode == 1
    assert unknown_member.stdout == ""
    assert unknown_member.stderr == "show-access: no such member in this org\n"

    other_member = _cli(
        url,
        "add-member",
        "--org-id",
        org_id,
        "--member-github-id",
        OTHER_ID,
    )
    assert other_member.returncode == 0, other_member.stderr
    nonmember_view = _cli(
        url,
        "show-access",
        "--org-id",
        str(uuid.uuid4()),  # a different org
        "--member-github-id",
        MEMBER_ID,
    )
    assert nonmember_view.returncode == 1
    assert nonmember_view.stderr == "show-access: no such member in this org\n"
    assert nonmember_view.stdout == ""
