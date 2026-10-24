"""The session-user investigation routes.

`POST /orgs/{org_id}/runs/{run_id}/investigation` and `.../investigation/brief`.
The workflow is the real `Investigation` (real `Assist`, `RunViewer` and
`Reliability`) over in-memory stores, so these tests cover the access chain,
non-disclosure, CSRF, provenance, and the fail-closed absence of providers.
`create_app` wires the routes; no lifespan and no database here.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from support import FAKE_DSN, RawResponse, send_raw

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import Investigation
from flakepilot.diagnosis.application.store import AiPolicy, DiagStore
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.identity.application.authorization import (
    Grant,
    OrgAccess,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import AccessVerification
from flakepilot.integrations.application.ports import (
    AiCapabilities,
    SpeechAudio,
    SpeechProviderError,
    SpeechRequest,
)
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.investigations import Investigations
from flakepilot.interface.http import auth
from flakepilot.shared_kernel.events import Event
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.run_view import RunViewer
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    RunSummary,
    TiStore,
)

ORIGIN = "https://fp.example"
COOKIE = auth.AuthConfig(ORIGIN, True).session_cookie
ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"
TEST_ID = "dddddddd-eeee-4fff-8aaa-000000000003"
USER = "investigator"
VIEWER = "viewer-only"
MEMBER_NO_GRANT = "member-without-grant"
ROGUE = "visitor-not-a-member"
SESSIONS = {f"sess-{u}-{'a' * 16}": u for u in (USER, VIEWER, MEMBER_NO_GRANT, ROGUE)}
SECRET_TEST_NAME = "test_login_reads_hunter2"


def session(user: str) -> str:
    return next(sid for sid, u in SESSIONS.items() if u == user)


class FakeRunStore:
    def __init__(self, stored: RunReport | None) -> None:
        self.stored = stored
        self.repo_lookups: list[tuple[str, str]] = []
        self.report_calls: list[tuple[str, str, str]] = []

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        self.repo_lookups.append((org_id, run_id))
        if self.stored is not None and (org_id, run_id) == (ORG, self.stored.run_id):
            return self.stored.repo_id
        return None

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        self.report_calls.append((org_id, repo_id, run_id))
        if self.stored is not None and (org_id, repo_id, run_id) == (ORG, REPO, RUN):
            return self.stored
        return None

    async def observations(self, **_: object) -> None:
        return None

    async def repo_history(self, **_: object) -> tuple[()]:
        return ()


class MemoryDiag:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def policy(self, org_id: str) -> AiPolicy:
        return AiPolicy(DataPolicy.DISABLED, False)

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None:
        raise AssertionError("not used")

    async def record_request(self, request: object) -> None:
        del request

    async def finish_request(self, request: object) -> None:
        del request

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


class FakeIam:
    def __init__(self, memberships: dict[str, list[tuple[str, Grant]]]) -> None:
        self._memberships = memberships

    async def authenticate(self, session_id: str) -> SessionPrincipal | None:
        user = SESSIONS.get(session_id)
        return SessionPrincipal(user) if user else None

    async def org_access(self, principal: SessionPrincipal, org_id: str) -> OrgAccess:
        grants = self._memberships.get(principal.user_id)
        if grants is None or org_id != ORG:
            raise ResourceNotFound
        return OrgAccess(principal.user_id, org_id, grants=frozenset(grants))

    async def login(self, principal: SessionPrincipal) -> str | None:
        return f"login-of-{principal.user_id}"


class FakeVerifier:
    def __init__(self, answer: AccessVerification) -> None:
        self._answer = answer

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification:
        return self._answer


class FakeSpeech:
    def __init__(self, error: SpeechProviderError | None = None) -> None:
        self.requests: list[SpeechRequest] = []
        self._error = error

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="speech_test")

    async def speak(self, request: SpeechRequest) -> SpeechAudio:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return SpeechAudio(
            audio=b"ID3fake",
            media_type="audio/mpeg",
            provider="speech_test",
            model=None,
        )


def stored_run() -> RunReport:
    return RunReport(
        run_id=RUN,
        org_id=ORG,
        repo_id=REPO,
        upload_id="cccccccc-dddd-4eee-8fff-000000000002",
        source="ci_upload",
        commit_sha="0" * 40,
        environment="ci-linux",
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
                classname="tests.auth",
                name=SECRET_TEST_NAME,
                attempts=("failed", "passed"),
                test_id=TEST_ID,
            ),
        ),
    )


MEMBERS = {
    USER: [(REPO, Grant.INVESTIGATE)],
    VIEWER: [(REPO, Grant.VIEW)],
    MEMBER_NO_GRANT: [],
}


def app_with(
    store: FakeRunStore,
    *,
    verifier: FakeVerifier | None = None,
    speech: FakeSpeech | None = None,
) -> tuple[FastAPI, MemoryDiag]:
    app = create_app(Settings(Secret(FAKE_DSN)))
    diag = MemoryDiag()

    @asynccontextmanager
    async def ti() -> AsyncIterator[TiStore]:
        yield cast(TiStore, store)

    @asynccontextmanager
    async def diag_uow() -> AsyncIterator[DiagStore]:
        yield diag

    iam = FakeIam(MEMBERS)
    app.state.auth_config = auth.AuthConfig(ORIGIN, True)
    app.state.iam = iam
    assist = Assist(diag_uow, NullAiAssist())
    app.state.investigations = Investigations(
        Investigation(
            runs=RunViewer(ti),
            assist=assist,
            reliability=Reliability(ti),
            unit_of_work=diag_uow,
        ),
        ti,
        verifier or FakeVerifier(AccessVerification.READ),
        iam.login,
        cast(Any, speech),
    )
    return app, diag


def post(
    app: FastAPI,
    *,
    user: str | None = USER,
    run_id: str = RUN,
    brief: bool = False,
    origin: str = ORIGIN,
) -> RawResponse:
    headers = [("origin", origin)]
    if user is not None:
        headers.append(("cookie", f"{COOKIE}={session(user)}"))
    target = f"/orgs/{ORG}/runs/{run_id}/investigation" + ("/brief" if brief else "")
    return asyncio.run(send_raw(app, "POST", target, headers))


def recorded(diag: MemoryDiag) -> list[str | None]:
    return [e.after for e in diag.events if e.name == "diag.investigation.recorded"]


def test_an_investigator_gets_the_report_and_provenance_is_recorded() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    status, headers, body = post(app)
    report = json.loads(body)
    assert status == 200
    assert dict(headers)["cache-control"] == "no-store"
    assert (report["org_id"], report["repo_id"], report["run_id"]) == (ORG, REPO, RUN)
    assert report["stopping"] == "complete"
    assert [(s["name"], s["status"], s["reason"]) for s in report["stages"]] == [
        ("collect", "produced", None),
        ("research", "skipped", "provider_absent"),
        ("browse", "skipped", "provider_absent"),
        ("hypothesize", "skipped", "policy_disabled"),
        ("prioritize", "skipped", "no_history_at_cutoff"),
    ]
    assert report["sources"] == report["pages"] == report["prioritized"] == []
    assert report["assist"]["outcome"] == "unavailable"
    assert report["brief"].startswith("FlakePilot investigation brief.")
    assert report["brief"].endswith("The full report is linked.")
    assert report["report_url"] == f"{ORIGIN}/orgs/{ORG}/runs/{RUN}"
    assert RUN not in report["brief"]
    assert SECRET_TEST_NAME not in report["brief"]
    assert recorded(diag) == ["complete"]


def test_view_without_investigate_is_forbidden_and_recorded() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    status, _, body = post(app, user=VIEWER)
    assert (status, json.loads(body)["error"]["code"]) == (403, "forbidden")
    assert store.report_calls == []  # refused before the run is read
    assert recorded(diag) == ["unauthorized"]


def test_a_member_without_grants_learns_nothing() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    status, _, body = post(app, user=MEMBER_NO_GRANT)
    assert (status, json.loads(body)["error"]["code"]) == (404, "not_found")
    assert store.report_calls == []
    assert recorded(diag) == ["not_found"]


def test_a_non_member_is_refused_before_any_store_read() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    status, _, body = post(app, user=ROGUE)
    assert (status, json.loads(body)["error"]["code"]) == (404, "not_found")
    assert store.repo_lookups == [] and diag.events == []


def test_an_unknown_run_and_a_foreign_run_answer_the_same() -> None:
    answers = []
    for stored in (None, stored_run()):
        app, diag = app_with(FakeRunStore(stored))
        answers.append(post(app, run_id="cccccccc-dddd-4eee-8fff-00000000000f"))
        assert diag.events == []
    assert {status for status, _, _ in answers} == {404}
    assert len({json.loads(body)["error"]["code"] for _, _, body in answers}) == 1


def test_unverified_code_host_standing_denies_without_disclosure() -> None:
    for answer in (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
        AccessVerification.NONE,
    ):
        store = FakeRunStore(stored_run())
        app, _ = app_with(store, verifier=FakeVerifier(answer))
        status, _, _ = post(app)
        assert status == 404, answer
        assert store.report_calls == []


def test_no_session_and_cross_site_posts_are_refused_before_any_read() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    assert post(app, user=None)[0] == 401
    status, _, body = post(app, origin="https://evil.example")
    assert (status, json.loads(body)["error"]["code"]) == (403, "forbidden")
    status, _, body = post(app, brief=True, origin="https://evil.example")
    assert status == 403
    assert store.repo_lookups == [] and diag.events == []


def test_a_non_uuid_run_is_a_validation_error_without_a_read() -> None:
    store = FakeRunStore(stored_run())
    app, _ = app_with(store)
    assert post(app, run_id="../../etc")[0] in (404, 422)
    assert post(app, run_id="x" * 36)[0] == 422
    assert store.repo_lookups == []


def test_the_brief_is_refused_when_no_speech_provider_is_configured() -> None:
    store = FakeRunStore(stored_run())
    app, diag = app_with(store)
    status, _, body = post(app, brief=True)
    assert (status, json.loads(body)["error"]["code"]) == (503, "speech_not_configured")
    assert store.repo_lookups == [] and diag.events == []  # no work, no provenance


def test_the_brief_speaks_counts_only_and_returns_audio_unstored() -> None:
    speech = FakeSpeech()
    app, diag = app_with(FakeRunStore(stored_run()), speech=speech)
    status, headers, body = post(app, brief=True)
    assert status == 200
    assert body == b"ID3fake"
    assert dict(headers)["content-type"] == "audio/mpeg"
    assert dict(headers)["cache-control"] == "no-store"
    assert dict(headers)["link"] == f'<{ORIGIN}/orgs/{ORG}/runs/{RUN}>; rel="related"'
    [spoken] = speech.requests
    assert spoken.text.startswith("FlakePilot investigation brief.")
    assert RUN not in spoken.text and ORIGIN not in spoken.text
    assert SECRET_TEST_NAME not in spoken.text and "hunter2" not in spoken.text
    assert recorded(diag) == ["complete"]


def test_the_brief_needs_investigate_and_never_speaks_otherwise() -> None:
    speech = FakeSpeech()
    app, _ = app_with(FakeRunStore(stored_run()), speech=speech)
    assert post(app, user=VIEWER, brief=True)[0] == 403
    assert post(app, user=MEMBER_NO_GRANT, brief=True)[0] == 404
    assert speech.requests == []


def test_a_speech_refusal_answers_its_short_code_only() -> None:
    speech = FakeSpeech(SpeechProviderError("quota_exceeded"))
    app, _ = app_with(FakeRunStore(stored_run()), speech=speech)
    status, _, body = post(app, brief=True)
    error = json.loads(body)["error"]
    assert (status, error["code"], error["reason"]) == (
        503,
        "speech_unavailable",
        "quota_exceeded",
    )
