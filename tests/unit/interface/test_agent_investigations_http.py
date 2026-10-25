"""The bound-agent investigation route (goal 9).

`POST /agent/orgs/{org_id}/runs/{run_id}/investigation` with the real
`Auth0TokenVerifier` (locally signed tokens, local key set), the real
`AgentInvestigations` chain and the real `Investigation` workflow over
in-memory stores. Covers mounting, every refusal and its status, non-
disclosure parity, bearer-only authentication, and provenance attribution.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from auth0_support import AUDIENCE, DOMAIN, OTHER_KEY, SUBJECT, local_verifier, mint
from auth0_support import tampered as tamper
from fastapi import FastAPI
from support import FAKE_DSN, RawResponse, send_raw

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import Investigation
from flakepilot.diagnosis.application.store import AiPolicy, DiagStore
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.identity.application.agent_bindings import (
    AgentBinding,
    AgentBindingStore,
    subject_digest,
)
from flakepilot.identity.application.authorization import Grant, OrgAccess
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import AccessVerification
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.agent_investigations import (
    AgentInvestigations,
)
from flakepilot.interface.http import auth
from flakepilot.shared_kernel.events import ActorType, Event
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.run_view import RunViewer
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    RunSummary,
    TiStore,
)

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_REPO = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"
FOREIGN_RUN = "cccccccc-dddd-4eee-8fff-000000000009"
ADMIN = "99999999-8888-4777-8666-555555555555"
SECRET_TEST_NAME = "test_login_reads_hunter2"
CONFIGURED = Settings(Secret(FAKE_DSN), auth0_domain=DOMAIN, auth0_audience=AUDIENCE)


def report(run_id: str, repo_id: str) -> RunReport:
    return RunReport(
        run_id=run_id,
        org_id=ORG,
        repo_id=repo_id,
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
                test_id="dddddddd-eeee-4fff-8aaa-000000000003",
            ),
        ),
    )


class FakeRunStore:
    def __init__(self) -> None:
        self.runs = {
            RUN: report(RUN, REPO),
            FOREIGN_RUN: report(FOREIGN_RUN, OTHER_REPO),
        }
        self.report_calls = 0

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        found = self.runs.get(run_id)
        return found.repo_id if found and org_id == ORG else None

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        self.report_calls += 1
        found = self.runs.get(run_id)
        return found if found and (org_id, repo_id) == (ORG, found.repo_id) else None

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


class FakeBindings:
    """Active bindings by subject hash, and the delegator's live standing."""

    def __init__(self) -> None:
        self.binding: AgentBinding | None = AgentBinding(
            binding_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            org_id=ORG,
            repo_id=REPO,
            delegator_id=ADMIN,
            agent_id="mastra_triage",
            grants=frozenset({Grant.VIEW, Grant.INVESTIGATE}),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.delegator: OrgAccess | None = OrgAccess(ADMIN, ORG, org_admin=True)
        self.lookups = 0

    async def active_agent_binding(
        self, *, org_id: str, subject_hash: bytes
    ) -> AgentBinding | None:
        self.lookups += 1
        if org_id != ORG or subject_hash != subject_digest(SUBJECT):
            return None
        return self.binding

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None:
        return self.delegator if (user_id, org_id) == (ADMIN, ORG) else None


class FakeVerifier:
    def __init__(self, answer: AccessVerification = AccessVerification.READ) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification:
        self.calls.append(repo_id)
        return self.answer


class Harness:
    def __init__(self, settings: Settings = CONFIGURED) -> None:
        self.app: FastAPI = create_app(settings)
        self.runs = FakeRunStore()
        self.diag = MemoryDiag()
        self.bindings = FakeBindings()
        self.verifier = FakeVerifier()

        @asynccontextmanager
        async def ti() -> AsyncIterator[TiStore]:
            yield cast(TiStore, self.runs)

        @asynccontextmanager
        async def diag_uow() -> AsyncIterator[DiagStore]:
            yield self.diag

        @asynccontextmanager
        async def binding_uow() -> AsyncIterator[AgentBindingStore]:
            yield cast(AgentBindingStore, self.bindings)

        async def login(principal: SessionPrincipal) -> str | None:
            return f"login-of-{principal.user_id}"

        self.app.state.agent_investigations = AgentInvestigations(
            tokens=local_verifier(),
            bindings=binding_uow,
            workflow=Investigation(
                runs=RunViewer(ti),
                assist=Assist(diag_uow, NullAiAssist()),
                reliability=Reliability(ti),
                unit_of_work=diag_uow,
            ),
            runs=ti,
            verifier=self.verifier,
            login_reader=login,
        )

    def post(
        self,
        token: str | None = None,
        *,
        run_id: str = RUN,
        org_id: str = ORG,
        headers: list[tuple[str, str]] | None = None,
    ) -> RawResponse:
        sent = list(headers or [])
        if token is not None:
            sent.append(("authorization", f"Bearer {token}"))
        target = f"/agent/orgs/{org_id}/runs/{run_id}/investigation"
        return asyncio.run(send_raw(self.app, "POST", target, sent))

    def recorded(self) -> list[Event]:
        return [e for e in self.diag.events if e.name == "diag.investigation.recorded"]


def error(response: RawResponse) -> tuple[int, dict[str, Any]]:
    status, _, body = response
    parsed = json.loads(body)["error"]
    parsed.pop("request_id", None)
    return status, parsed


def test_a_bound_agent_gets_the_report_attributed_to_the_delegating_admin() -> None:
    harness = Harness()
    status, headers, body = harness.post(mint())
    assert status == 200
    assert dict(headers)["cache-control"] == "no-store"
    answer = json.loads(body)
    assert (answer["org_id"], answer["repo_id"], answer["run_id"]) == (ORG, REPO, RUN)
    assert answer["stopping"] == "complete"
    assert answer["report_url"] == f"http://testserver/orgs/{ORG}/runs/{RUN}"
    assert SECRET_TEST_NAME not in answer["brief"]
    [event] = harness.recorded()
    assert (event.actor.type, event.actor.id) == (ActorType.USER, ADMIN)
    assert event.payload["via_agent"] == "mastra_triage"
    assert event.after == "complete" and event.repo_id == REPO
    assert harness.verifier.calls == [REPO]


def test_the_report_url_uses_the_public_origin_when_configured() -> None:
    settings = Settings(
        Secret(FAKE_DSN),
        auth0_domain=DOMAIN,
        auth0_audience=AUDIENCE,
        public_origin="https://fp.example",
    )
    status, _, body = Harness(settings).post(mint())
    assert status == 200
    assert json.loads(body)["report_url"] == f"https://fp.example/orgs/{ORG}/runs/{RUN}"


def test_unconfigured_auth0_means_the_route_does_not_exist() -> None:
    app = create_app(Settings(Secret(FAKE_DSN)))
    target = f"/agent/orgs/{ORG}/runs/{RUN}/investigation"
    headers = [("authorization", f"Bearer {mint()}")]
    agent = asyncio.run(send_raw(app, "POST", target, headers))
    unknown = asyncio.run(send_raw(app, "POST", "/agent/unknown", headers))
    assert error(agent) == error(unknown)
    assert agent[0] == 404


def test_missing_malformed_and_refused_tokens_are_one_generic_401() -> None:
    harness = Harness()
    answers = [
        harness.post(None),
        harness.post(headers=[("authorization", "Basic abc")]),
        harness.post(headers=[("authorization", "Bearer ")]),
        harness.post("not-a-jwt"),
        harness.post(mint(audience="https://wrong.flakepilot.invalid")),
        harness.post(mint(expires_in=-60)),
        harness.post(tamper(mint())),
        harness.post(mint(key=OTHER_KEY)),
    ]
    bodies = [error(answer) for answer in answers]
    assert {status for status, _ in bodies} == {401}
    assert len({json.dumps(body, sort_keys=True) for _, body in bodies}) == 1
    assert harness.bindings.lookups == 0 and harness.diag.events == []


def test_a_session_cookie_is_never_a_credential_here() -> None:
    harness = Harness()
    harness.app.state.auth_config = auth.AuthConfig("https://fp.example", True)
    cookie = auth.AuthConfig("https://fp.example", True).session_cookie
    status, _ = error(
        harness.post(
            headers=[
                ("cookie", f"{cookie}={'a' * 43}"),
                ("origin", "https://fp.example"),
            ]
        )
    )
    assert status == 401


def test_every_not_visible_outcome_is_the_same_404_without_provenance() -> None:
    answers = []

    harness = Harness()  # a subject with no binding
    answers.append(harness.post(mint(subject="otherclient@clients")))
    harness = Harness()  # revoked or expired: the store returns nothing active
    harness.bindings.binding = None
    answers.append(harness.post(mint()))
    harness = Harness()  # the delegating admin is no longer a member
    harness.bindings.delegator = None
    answers.append(harness.post(mint()))
    harness = Harness()  # another org in the path
    answers.append(harness.post(mint(), org_id="11111111-2222-4333-8444-666666666666"))
    harness = Harness()  # an unknown run
    answers.append(harness.post(mint(), run_id="cccccccc-dddd-4eee-8fff-00000000000f"))
    harness = Harness()  # a run in another repository of the org
    answers.append(harness.post(mint(), run_id=FOREIGN_RUN))
    assert harness.verifier.calls == []  # refused before any code-host call
    assert harness.runs.report_calls == 0
    harness = Harness()  # the admin was demoted to a plain member
    harness.bindings.delegator = OrgAccess(ADMIN, ORG)
    answers.append(harness.post(mint()))
    for unverified in (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
        AccessVerification.NONE,
    ):
        harness = Harness()
        harness.verifier.answer = unverified
        answers.append(harness.post(mint()))
        assert harness.diag.events == []

    unknown = asyncio.run(send_raw(harness.app, "POST", "/agent/unknown", []))
    bodies = {json.dumps(error(answer), sort_keys=True) for answer in answers}
    assert bodies == {json.dumps(error(unknown), sort_keys=True)}


def test_a_visible_run_without_investigate_is_403() -> None:
    for scope in ("repo.view", "repo.approve repo.configure"):
        harness = Harness()
        status, body = error(harness.post(mint(scope=scope)))
        assert (status, body["code"]) == (403, "forbidden")
        assert harness.runs.report_calls == 0
    harness = Harness()
    assert harness.bindings.binding is not None
    harness.bindings.binding = AgentBinding(
        **{**harness.bindings.binding.__dict__, "grants": frozenset({Grant.VIEW})}
    )
    assert error(harness.post(mint()))[0] == 403


def test_a_non_uuid_path_is_a_validation_error_before_any_token_work() -> None:
    harness = Harness()
    assert harness.post(mint(), run_id="x" * 36)[0] == 422
    assert harness.bindings.lookups == 0
