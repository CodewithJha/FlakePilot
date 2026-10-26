"""The session-user runs listing (GET /orgs/{org_id}/repos/{repo_id}/runs).

Fakes carry IAM's and REPO's contracts exactly as the real assemblies do, and
the route runs through the real composition chain (`RunViews.runs_index`). It
is the same fail-closed, non-disclosing chain as the single-run view, mapping
one added honest behavior: an authorized pair with no stored runs is an empty
page, not an error. No database here; these are automated checks with fakes.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from fastapi import FastAPI
from support import FAKE_DSN, RawResponse, send_raw

from flakepilot.identity.application.authorization import (
    Grant,
    OrgAccess,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import AccessVerification
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.run_views import RunViews
from flakepilot.interface.http import auth
from flakepilot.test_intelligence.application.store import (
    RunIndexEntry,
    RunSummary,
    TiStore,
)

SETTINGS = Settings(Secret(FAKE_DSN))
COOKIE = auth.AuthConfig("https://fp.example", True).session_cookie

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"
USER = "user-with-view"
ROGUE_USER = "visitor-not-a-member"
SESSION = "sess-" + "a" * 20


def entry(
    run: str = RUN,
    *,
    environment: str = "demo.local",
    source: str = "ci_upload",
    commit: str = "0" * 40,
) -> RunIndexEntry:
    return RunIndexEntry(
        run_id=run,
        source=source,
        commit_sha=commit,
        environment=environment,
        revision_status="unchecked",
        received_at=datetime(2026, 10, 23, tzinfo=UTC),
        summary=RunSummary(
            tests=2, attempts=3, passed=1, failed=1, errored=0, skipped=1
        ),
    )


class FakeRunStore:
    """runs_index on the store's own (org, repo) scope; the read path is inert."""

    def __init__(self, entries: tuple[RunIndexEntry, ...]) -> None:
        self.entries = entries
        self.index_calls: list[tuple[str, str, int]] = []

    async def append_event(self, event: object) -> None: ...

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        raise AssertionError("a listing never probes single runs")

    async def run_report(self, **kwargs: object) -> object:
        raise AssertionError("a listing never reads full reports")

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[object] | None:
        raise AssertionError("a listing computes no classifications")

    async def runs_index(
        self, *, org_id: str, repo_id: str, limit: int
    ) -> tuple[RunIndexEntry, ...]:
        self.index_calls.append((org_id, repo_id, limit))
        return self.entries


class FakeIam:
    """authenticate + org_access, with the real contract (not a member: not found)."""

    def __init__(
        self,
        sessions: dict[str, str],
        memberships: dict[tuple[str, str], list[tuple[str, Grant]]],
    ) -> None:
        self._sessions = sessions
        self._memberships = memberships
        self.authenticated: list[str] = []
        self.access_calls: list[tuple[str, str]] = []

    async def authenticate(self, session_id: str) -> SessionPrincipal | None:
        self.authenticated.append(session_id)
        user_id = self._sessions.get(session_id)
        return SessionPrincipal(user_id) if user_id else None

    async def org_access(self, principal: SessionPrincipal, org_id: str) -> OrgAccess:
        self.access_calls.append((principal.user_id, org_id))
        grants = self._memberships.get((principal.user_id, org_id))
        if grants is None:
            raise ResourceNotFound
        return OrgAccess(
            principal.user_id, org_id, org_admin=False, grants=frozenset(grants)
        )

    async def login(self, principal: SessionPrincipal) -> str | None:
        return f"login-of-{principal.user_id}"


class FakeVerifier:
    """REPO's verifier contract, canned. Records the lookups the flow makes."""

    def __init__(self, answer: AccessVerification) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str, str]] = []

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification:
        self.calls.append((login, org_id, repo_id))
        return self._answer


def app_with(
    store: FakeRunStore,
    iam: FakeIam,
    verifier: FakeVerifier | None = None,
) -> FastAPI:
    app = create_app(SETTINGS)

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        yield cast(TiStore, store)

    app.state.auth_config = auth.AuthConfig("https://fp.example", True)
    app.state.iam = iam
    app.state.run_views = RunViews(
        unit_of_work,
        verifier if verifier is not None else FakeVerifier(AccessVerification.READ),
        iam.login,
    )
    return app


def fetch(app: FastAPI, target: str, session: str | None = SESSION) -> RawResponse:
    headers = [] if session is None else [("cookie", f"{COOKIE}={session}")]
    return asyncio.run(send_raw(app, "GET", target, headers))


def get_index(
    app: FastAPI, *, repo: str = REPO, org: str = ORG, session: str | None = SESSION
) -> RawResponse:
    return fetch(app, f"/orgs/{org}/repos/{repo}/runs", session)


def test_an_authorized_member_gets_the_listing_with_a_link_to_each_run() -> None:
    store = FakeRunStore(
        (
            entry(environment="demo & <staging>"),
            entry(run=RUN[:-1] + "2"),
        )
    )
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_index(app_with(store, iam))
    text = body.decode()
    assert status == 200
    assert "FlakePilot runs" in text and "newest first" in text
    assert "demo & \\u003cstaging\\u003e" in text
    assert "<staging>" not in text
    assert f'href="/orgs/{ORG}/runs/{RUN}"' in text or (
        f'"href":"/orgs/{ORG}/runs/{RUN}"' in text
    )
    assert "unchecked" in text
    assert iam.authenticated == [SESSION] and iam.access_calls == [(USER, ORG)]
    assert store.index_calls == [(ORG, REPO, 50)]


def test_an_empty_pair_is_an_honest_empty_page() -> None:
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_index(app_with(FakeRunStore(()), iam))
    text = body.decode()
    assert status == 200
    assert "No stored runs for this repository yet" in text
    assert "There may be runs you cannot see" in text


def test_a_member_without_repo_view_is_denied_as_not_found() -> None:
    """A grant-less member learns nothing: NOT_FOUND, and nothing is listed."""
    member_no_grant = "member-without-grant"
    store = FakeRunStore((entry(),))
    iam = FakeIam({SESSION: member_no_grant}, {(member_no_grant, ORG): []})
    status, _, body = get_index(app_with(store, iam))
    assert status == 404 and '"not_found"' in body.decode()
    # The decision happened before any listing query.
    assert store.index_calls == []
    assert iam.access_calls == [(member_no_grant, ORG)]


def test_a_non_member_and_an_unbound_pair_answer_the_same_not_found() -> None:
    """Non-disclosure: not a member and an unbound/foreign repo are
    indistinguishable 404s; an authorized empty pair is 200, not an error."""
    iam = FakeIam({SESSION: ROGUE_USER}, {})
    rogue = get_index(app_with(FakeRunStore((entry(),)), iam))
    # A repo bound to another org (or absent) is UNBOUND to REPO's verifier:
    # the same 404, and the listing is never read.
    unbound = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    other = "bbbbbbbb-bbbb-4ddd-8eee-000000000004"
    foreign_store = FakeRunStore((entry(),))
    foreign = fetch(
        app_with(
            foreign_store,
            unbound,
            FakeVerifier(AccessVerification.UNBOUND),
        ),
        f"/orgs/{ORG}/repos/{other}/runs",
    )
    assert rogue[0] == foreign[0] == 404
    for body in (rogue[2], foreign[2]):
        assert '"not_found"' in body.decode()
    # The foreign pair was denied during verification, not by listing it.
    assert foreign_store.index_calls == []


def test_an_unverified_or_unavailable_standing_denies_without_a_store_read() -> None:
    """Fail closed: every unverified outcome denies, and no listing runs."""
    for answer in (
        AccessVerification.INDETERMINATE,
        AccessVerification.UNAVAILABLE,
        AccessVerification.UNBOUND,
        AccessVerification.NONE,
    ):
        store = FakeRunStore((entry(),))
        iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
        status, _, body = get_index(app_with(store, iam, FakeVerifier(answer)))
        assert status == 404 and '"not_found"' in body.decode()
        assert store.index_calls == []


def test_a_missing_cookie_is_unauthenticated() -> None:
    store = FakeRunStore(())
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_index(app_with(store, iam), session=None)
    assert status == 401 and '"unauthenticated"' in body.decode()
    assert iam.access_calls == [] and store.index_calls == []


def test_a_non_canonical_repo_id_is_not_found_without_a_store_read() -> None:
    """Route lookup keys follow the run view's own rules: garbage in the URL
    is the same not found, decided before the stored data is consulted."""
    store = FakeRunStore((entry(),))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_index(
        app_with(store, iam),
        repo="x" * 36,
    )
    assert status == 404 and '"not_found"' in body.decode()
    assert store.index_calls == []


def test_the_limit_is_a_page_size_never_an_authorization_parameter() -> None:
    """The listing clamps the limit in the use case; grants decide visibility."""
    store = FakeRunStore(())
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, _ = fetch(
        app_with(store, iam), f"/orgs/{ORG}/repos/{REPO}/runs?limit=100000"
    )
    assert status == 200
    assert store.index_calls == [(ORG, REPO, 200)]


def test_a_listed_run_in_the_report_is_never_classified_inline() -> None:
    """The listing carries exactly the row's own facts; verdicts live on the run's view."""
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    text = get_index(app_with(FakeRunStore((entry(),)), iam))[2].decode()
    assert "ti-classification/1" in text  # the rule set is named, not computed
    assert "flaky" not in text
