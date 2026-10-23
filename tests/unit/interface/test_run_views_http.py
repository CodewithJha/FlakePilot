"""The session-user run view (GET /orgs/{org_id}/runs/{run_id}).

Fakes carry the IAM contract (authenticate/org_access) exactly as the real
`Iam` implements it: the org ID is only a lookup key and access comes from
IAM's records for the verified session principal. The classification and
evidence run through the real code path (real `RunViewer` and the domain rule
set over a fake store), so these tests cover policy, non-disclosure, and
rendering. `create_app` wires everything; no lifespan and no database here.
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
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.run_views import RunViews
from flakepilot.interface.http import auth
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    RunSummary,
    TiStore,
)
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

SETTINGS = Settings(Secret(FAKE_DSN))
COOKIE = auth.AuthConfig("https://fp.example", True).session_cookie

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"
TEST_ID = "dddddddd-eeee-4fff-8aaa-000000000003"
USER = "user-with-view"
MEMBER_NO_GRANT = "member-without-grant"
ROGUE_USER = "visitor-not-a-member"
SESSION = "sess-" + "a" * 20
UNKNOWN_SESSION = "sess-" + "e" * 20


def report(name: str) -> RunReport:
    return RunReport(
        run_id=RUN,
        org_id=ORG,
        repo_id=REPO,
        upload_id="cccccccc-dddd-4eee-8fff-000000000002",
        source="ci_upload",
        commit_sha="0" * 40,
        environment="demo.local",
        revision_status="unchecked",
        received_at=datetime(2026, 10, 23, tzinfo=UTC),
        summary=RunSummary(
            tests=1, attempts=2, passed=1, failed=0, errored=0, skipped=0
        ),
        metadata=None,
        content_sha256=None,
        size_bytes=None,
        artifact_key=None,
        results=(
            ReportedCase(
                classname="demo.auth",
                name=name,
                attempts=("failed", "passed"),
                test_id=TEST_ID,
            ),
        ),
    )


class FakeRunStore:
    """run_repo_id + run_report + observations, scoped exactly like the real store."""

    def __init__(self, stored: RunReport | None, *, with_evidence: bool = True) -> None:
        self.stored = stored
        self.with_evidence = with_evidence
        self.repo_lookups: list[tuple[str, str]] = []
        self.report_calls: list[tuple[str, str, str]] = []
        self.observed: list[str] = []

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        self.repo_lookups.append((org_id, run_id))
        if self.stored is not None and (org_id, run_id) == (
            self.stored.org_id,
            self.stored.run_id,
        ):
            return self.stored.repo_id
        return None

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        self.report_calls.append((org_id, repo_id, run_id))
        if self.stored is not None and (org_id, repo_id, run_id) == (
            ORG,
            REPO,
            self.stored.run_id,
        ):
            return self.stored
        return None

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[Observation] | None:
        self.observed.append(test_id)
        if not self.with_evidence:
            return None
        return [
            Observation(
                run_id="run<b>",
                commit_sha="0" * 40,
                environment="demo.local",
                received_at=datetime(2026, 10, 23, tzinfo=UTC),
                source=RunSource.CI_UPLOAD,
                attempts=(Outcome("failed"), Outcome("passed")),
            )
        ]


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


def app_with(store: FakeRunStore, iam: FakeIam) -> FastAPI:
    app = create_app(SETTINGS)

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        yield cast(TiStore, store)

    # Same wiring the lifespan does when sign-in is configured.
    app.state.auth_config = auth.AuthConfig("https://fp.example", True)
    app.state.iam = iam
    app.state.run_views = RunViews(unit_of_work)
    return app


def fetch(app: FastAPI, target: str, session: str | None = SESSION) -> RawResponse:
    headers = [] if session is None else [("cookie", f"{COOKIE}={session}")]
    return asyncio.run(send_raw(app, "GET", target, headers))


def get_run(
    app: FastAPI, *, run_id: str = RUN, org: str = ORG, session: str | None = SESSION
) -> RawResponse:
    return fetch(app, f"/orgs/{org}/runs/{run_id}", session)


def test_an_authorized_member_gets_the_server_rendered_run_view() -> None:
    store = FakeRunStore(report("test_login & <totals>"))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_run(app_with(store, iam))
    text = body.decode()
    assert status == 200
    assert "FlakePilot run view" in text
    assert "test_login &amp; &lt;totals&gt;" in text  # the test name is escaped
    assert "<td>failed &rarr; passed</td>" in text  # the attempt chain intact
    assert "flaky" in text  # the real rule set's verdict, through the real viewer
    assert "rule ti-classification/1" in text
    assert "run&lt;b&gt;" in text  # evidence values are escaped too
    # The chain went cookie -> session principal -> IAM's org records -> store.
    assert iam.authenticated == [SESSION]
    assert iam.access_calls == [(USER, ORG)]
    assert store.repo_lookups == [(ORG, RUN)]
    assert store.report_calls == [(ORG, REPO, RUN)]


def test_the_view_states_what_it_is_not() -> None:
    text = get_run(
        app_with(
            FakeRunStore(report("x")),
            FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]}),
        )
    )[2].decode()
    assert "unchecked" in text
    assert "no confidence scores, no model output" in text
    assert 'href="/auth/github/login"' not in text  # no navigation beyond this page


def test_a_member_without_repo_view_is_denied_as_not_found() -> None:
    """A grant-less member learns nothing: NOT_FOUND, and the run is never read."""
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: MEMBER_NO_GRANT}, {(MEMBER_NO_GRANT, ORG): []})
    status, _, body = get_run(app_with(store, iam))
    assert (status, body.decode().count('"not_found"')) == (404, 1)
    # The decision happened after the org-scoped run lookup, but before any
    # report read or classification work.
    assert store.repo_lookups == [(ORG, RUN)]
    assert store.report_calls == [] and store.observed == []
    assert iam.access_calls == [(MEMBER_NO_GRANT, ORG)]


def test_a_non_member_of_the_org_gets_the_same_not_found() -> None:
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: ROGUE_USER}, {})
    status, _, body = get_run(app_with(store, iam))
    assert (status, body.decode().count('"not_found"')) == (404, 1)
    assert store.repo_lookups == [] and store.report_calls == []


def test_a_run_of_another_org_is_indistinguishable_from_an_unknown_run() -> None:
    """Non-disclosure: a foreign run and an unknown run answer the same 404."""
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    foreign = get_run(
        app_with(FakeRunStore(None), iam),
        run_id="cccccccc-dddd-4eee-8fff-000000000002",
    )
    unknown = get_run(
        app_with(FakeRunStore(None), iam),
        run_id="cccccccc-dddd-4eee-8fff-000000000003",
    )
    assert foreign[0] == unknown[0] == 404
    assert '"not_found"' in foreign[2].decode() and '"not_found"' in unknown[2].decode()


def test_a_garbage_session_cookie_is_not_a_session() -> None:
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_run(app_with(store, iam), session="not-a-real-session")
    assert (status, body.decode().count('"unauthenticated"')) == (401, 1)
    assert store.repo_lookups == [] and store.report_calls == []


def test_a_missing_cookie_is_unauthenticated() -> None:
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_run(app_with(store, iam), session=None)
    assert (status, body.decode().count('"unauthenticated"')) == (401, 1)
    assert store.repo_lookups == [] and store.report_calls == []


def test_missing_classification_evidence_says_so() -> None:
    """An identity without stored history renders 'classification unavailable'."""
    store = FakeRunStore(report("test_login_succeeds"), with_evidence=False)
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_run(app_with(store, iam))
    text = body.decode()
    assert status == 200
    assert "classification unavailable" in text
    assert "flaky" not in text
    assert store.observed == [TEST_ID]


def test_a_non_canonical_run_id_is_not_found_without_a_store_read() -> None:
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, body = get_run(app_with(store, iam), run_id="x" * 36)
    assert (status, body.decode().count('"not_found"')) == (404, 1)
    assert store.report_calls == []


def test_a_non_canonical_org_in_the_url_is_a_validation_error() -> None:
    """The route's UUID constraint rejects the path before the org is a key."""
    store = FakeRunStore(report("x"))
    iam = FakeIam({SESSION: USER}, {(USER, ORG): [(REPO, Grant.VIEW)]})
    status, _, _body = get_run(app_with(store, iam), org="zz")
    assert status == 422
    assert store.repo_lookups == [] and store.report_calls == []
