"""Auth0 agent bindings and the agent investigation route over real PostgreSQL (goal 9).

Bindings are made and revoked through the operator command functions; the
route runs through `create_app` with the real `AgentInvestigations` over real
`iam`, `ti` and `diag` tables. Tokens are locally signed and checked by the
real `Auth0TokenVerifier` against a local key set; the delegator's code-host
standing comes from a `verified_read` stand-in (no GitHub App here).
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, Seeded, seed, seed_run
from auth0_support import AUDIENCE, DOMAIN, SUBJECT, local_verifier, mint, tampered
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from support import send_raw

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import Investigation
from flakepilot.identity.application.agents import AgentRefused
from flakepilot.identity.application.authorization import PermissionDenied
from flakepilot.identity.application.iam import Iam
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.interface.composition_root import (
    Secret,
    Settings,
    create_app,
    diag_unit_of_work,
    iam_unit_of_work,
    ti_unit_of_work,
)
from flakepilot.interface.composition_root.agent_binding_commands import (
    bind,
    run_bind_agent,
    run_unbind_agent,
    unbind,
)
from flakepilot.interface.composition_root.agent_investigations import (
    AgentInvestigations,
    binding_unit_of_work,
)
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.run_view import RunViewer

pytestmark = pytest.mark.integration


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
        if isinstance(result, CursorResult) and result.returns_rows:
            return list(result)
        return []


def settings(url: str) -> Settings:
    return Settings(Secret(url), auth0_domain=DOMAIN, auth0_audience=AUDIENCE)


async def bind_default(url: str, seeded: Seeded, **overrides: Any) -> tuple[str, Any]:
    arguments: dict[str, Any] = {
        "org_id": seeded.org_id,
        "github_id": ADMIN_GITHUB_ID,
        "repo_id": seeded.repo_id,
        "subject": SUBJECT,
        "agent_name": "mastra_triage",
        "scopes": ["repo.view", "repo.investigate"],
        "ttl": timedelta(minutes=30),
        **overrides,
    }
    return await bind(settings(url), **arguments)


def agent_app(engine: AsyncEngine, url: str) -> tuple[Any, ReadVerifier]:
    app = create_app(settings(url))
    verifier = ReadVerifier()
    diag, ti = diag_unit_of_work(engine), ti_unit_of_work(engine)
    app.state.agent_investigations = AgentInvestigations(
        tokens=local_verifier(),
        bindings=binding_unit_of_work(engine),
        workflow=Investigation(
            runs=RunViewer(ti),
            assist=Assist(diag, NullAiAssist()),
            reliability=Reliability(ti),
            unit_of_work=diag,
        ),
        runs=ti,
        verifier=verifier,
        login_reader=Iam(iam_unit_of_work(engine), None).login,
    )
    return app, verifier


async def post(
    app: Any, token: str | None, org_id: str, run_id: str
) -> tuple[int, dict[str, Any]]:
    headers = [] if token is None else [("authorization", f"Bearer {token}")]
    status, _, body = await send_raw(
        app, "POST", f"/agent/orgs/{org_id}/runs/{run_id}/investigation", headers
    )
    parsed = json.loads(body)
    if "error" in parsed:
        parsed["error"].pop("request_id", None)
    return status, parsed


def test_binding_is_stored_as_a_hash_audited_and_revocable(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        seeded = await seed(engine)
        binding_id, expires_at = await bind_default(url, seeded)
        try:
            await bind_default(url, seeded)
        except AgentRefused as refused:
            second = refused.reason
        rows = await sql(
            engine,
            "SELECT id, delegator_id, repo_id, agent_id, grants, subject_hash, "
            "expires_at - created_at AS ttl, revoked_at "
            "FROM iam.agent_bindings WHERE org_id = :org",
            org=seeded.org_id,
        )
        outcome = await unbind(
            settings(url),
            org_id=seeded.org_id,
            github_id=ADMIN_GITHUB_ID,
            subject=SUBJECT,
        )
        repeat = await unbind(
            settings(url),
            org_id=seeded.org_id,
            github_id=ADMIN_GITHUB_ID,
            subject=SUBJECT,
        )
        rebound, _ = await bind_default(url, seeded)
        events = await sql(
            engine,
            "SELECT name, actor_type, actor_id, subject_id, after, payload::text AS p "
            "FROM iam.outbox WHERE org_id = :org AND name LIKE 'iam.agent.%' "
            "ORDER BY seq",
            org=seeded.org_id,
        )
        return (
            seeded,
            binding_id,
            expires_at,
            second,
            rows,
            outcome,
            repeat,
            rebound,
            events,
        )

    seeded, binding_id, _expires_at, second, rows, outcome, repeat, rebound, events = (
        run(url, scenario)
    )
    assert second == "already_bound"
    [row] = rows
    assert str(row.id) == binding_id and str(row.delegator_id) == seeded.admin_id
    assert str(row.repo_id) == seeded.repo_id and row.agent_id == "mastra_triage"
    assert sorted(row.grants) == ["repo.investigate", "repo.view"]
    assert len(row.subject_hash) == 32 and SUBJECT.encode() not in row.subject_hash
    assert row.ttl == timedelta(minutes=30) and row.revoked_at is None
    assert (outcome, repeat) == ("revoked", "not_bound")
    assert rebound != binding_id
    assert [(e.name, e.after) for e in events] == [
        ("iam.agent.bound", "bound"),
        ("iam.agent.unbound", "revoked"),
        ("iam.agent.bound", "bound"),
    ]
    assert {(e.actor_type, str(e.actor_id)) for e in events} == {
        ("user", seeded.admin_id)
    }
    assert str(events[0].subject_id) == binding_id
    assert all("agentclient123" not in e.p for e in events)


def test_only_an_org_admin_binds_and_the_schema_caps_scope_and_ttl(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        seeded = await seed(engine)
        await sql(
            engine,
            "UPDATE iam.memberships SET org_admin = false WHERE org_id = :org",
            org=seeded.org_id,
        )
        try:
            await bind_default(url, seeded)
        except PermissionDenied:
            refused = "not_admin"
        violations = []
        for grants, ttl in (
            (["repo.approve"], timedelta(hours=1)),
            ([], timedelta(hours=1)),
            (["repo.view"], timedelta(hours=9)),
        ):
            try:
                await sql(
                    engine,
                    "INSERT INTO iam.agent_bindings (id, org_id, delegator_id, "
                    "repo_id, subject_hash, agent_id, grants, created_at, "
                    "expires_at) VALUES (:id, :org, :user, :repo, "
                    "decode(repeat('ab', 32), 'hex'), 'x', CAST(:grants AS text[]), "
                    "now(), now() + :ttl)",
                    id=str(uuid.uuid4()),
                    org=seeded.org_id,
                    user=seeded.admin_id,
                    repo=seeded.repo_id,
                    grants=grants,
                    ttl=ttl,
                )
            except IntegrityError:
                violations.append(tuple(grants))
        return refused, violations

    refused, violations = run(url, scenario)
    assert refused == "not_admin"
    assert violations == [("repo.approve",), (), ("repo.view",)]


def test_the_cli_commands_bind_print_no_subject_and_refuse_bad_input(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded = run(url, seed)
    base = ["--org-id", seeded.org_id, "--admin-github-id", ADMIN_GITHUB_ID]
    subject = ["--auth0-subject", SUBJECT]
    bind_args = [
        *base,
        *subject,
        "--repo-id",
        seeded.repo_id,
        "--agent-name",
        "mastra_triage",
        "--scope",
        "repo.investigate",
        "--ttl-minutes",
        "30",
    ]
    assert run_bind_agent(settings(url), bind_args) == 0
    printed = capsys.readouterr()
    assert printed.out.startswith("bound ") and "agentclient123" not in printed.out
    assert run_bind_agent(settings(url), bind_args) == 1
    assert "refused: already_bound" in capsys.readouterr().err
    too_long = [*bind_args[:-1], "481"]
    assert run_bind_agent(settings(url), too_long) == 1
    other_repo = [*bind_args]
    other_repo[other_repo.index(seeded.repo_id)] = str(uuid.uuid4())
    assert run_bind_agent(settings(url), other_repo) == 1
    assert "registered repository" in capsys.readouterr().err
    assert run_unbind_agent(settings(url), [*base, *subject]) == 0
    assert capsys.readouterr().out.strip() == "revoked"


def test_the_route_serves_a_bound_agent_and_records_attributed_provenance(
    url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        seeded = await seed(engine)
        await bind_default(url, seeded)
        app, verifier = agent_app(engine, url)
        answer = await post(app, mint(), seeded.org_id, seeded.run_id)
        rows = await sql(
            engine,
            "SELECT actor_type, actor_id, after, repo_id, payload->>'via_agent' AS via "
            "FROM diag.outbox WHERE org_id = :org "
            "AND name = 'diag.investigation.recorded'",
            org=seeded.org_id,
        )
        return seeded, answer, rows, verifier.calls

    seeded, (status, body), rows, calls = run(url, scenario)
    assert status == 200
    assert body["stopping"] == "complete" and body["run_id"] == seeded.run_id
    assert [s["name"] for s in body["stages"]] == [
        "collect",
        "research",
        "browse",
        "hypothesize",
        "prioritize",
    ]
    [row] = rows
    assert (row.actor_type, str(row.actor_id)) == ("user", seeded.admin_id)
    assert row.via == "mastra_triage" and row.after == "complete"
    assert str(row.repo_id) == seeded.repo_id
    assert calls == [(seeded.org_id, seeded.repo_id)]


def test_every_refusal_on_real_tables_and_the_404s_are_identical(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        seeded = await seed(engine)
        other_repo_run = await seed_run(
            engine, org_id=seeded.org_id, repo_id=str(uuid.uuid4())
        )
        await bind_default(url, seeded)
        app, verifier = agent_app(engine, url)
        org, run_id = seeded.org_id, seeded.run_id
        answers: dict[str, tuple[int, dict[str, Any]]] = {
            "no_token": await post(app, None, org, run_id),
            "wrong_audience": await post(
                app, mint(audience="https://other.example"), org, run_id
            ),
            "expired": await post(app, mint(expires_in=-30), org, run_id),
            "tampered": await post(app, tampered(mint()), org, run_id),
            "view_only_token": await post(app, mint(scope="repo.view"), org, run_id),
            "unbound_subject": await post(
                app, mint(subject="stranger@clients"), org, run_id
            ),
            "other_org": await post(app, mint(), str(uuid.uuid4()), run_id),
            "unknown_run": await post(app, mint(), org, str(uuid.uuid4())),
            "other_repo_run": await post(app, mint(), org, other_repo_run),
        }
        await unbind(
            settings(url), org_id=org, github_id=ADMIN_GITHUB_ID, subject=SUBJECT
        )
        answers["revoked"] = await post(app, mint(), org, run_id)
        await bind_default(url, seeded)
        await sql(
            engine,
            "UPDATE iam.agent_bindings SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 second' WHERE org_id = :org "
            "AND revoked_at IS NULL",
            org=org,
        )
        answers["expired_binding"] = await post(app, mint(), org, run_id)
        await sql(
            engine,
            "UPDATE iam.agent_bindings SET revoked_at = now() WHERE org_id = :org",
            org=org,
        )
        await bind_default(url, seeded)
        await sql(
            engine,
            "UPDATE iam.memberships SET org_admin = false WHERE org_id = :org",
            org=org,
        )
        answers["demoted_admin"] = await post(app, mint(), org, run_id)
        await sql(engine, "DELETE FROM iam.memberships WHERE org_id = :org", org=org)
        answers["removed_admin"] = await post(app, mint(), org, run_id)
        cascaded = await sql(
            engine,
            "SELECT count(*) AS n FROM iam.agent_bindings WHERE org_id = :org",
            org=org,
        )
        unknown = await send_raw(app, "POST", "/agent/unknown", [])
        recorded = await sql(
            engine,
            "SELECT after FROM diag.outbox WHERE org_id = :org "
            "AND name = 'diag.investigation.recorded'",
            org=org,
        )
        return answers, cascaded[0].n, json.loads(unknown[2]), recorded, verifier

    answers, remaining, unknown, recorded, verifier = run(url, scenario)
    unknown["error"].pop("request_id", None)
    for name in ("no_token", "wrong_audience", "expired", "tampered"):
        assert answers[name] == (401, answers["no_token"][1]), name
        assert answers[name][1]["error"]["code"] == "unauthenticated"
    assert answers["view_only_token"][0] == 403
    assert answers["view_only_token"][1]["error"]["code"] == "forbidden"
    hidden = (
        "unbound_subject",
        "other_org",
        "unknown_run",
        "other_repo_run",
        "revoked",
        "expired_binding",
        "demoted_admin",
        "removed_admin",
    )
    for name in hidden:
        assert answers[name] == (404, unknown), name
    assert remaining == 0  # removing the membership cascades to its bindings
    # Refusals before the workflow write nothing; the 403 is recorded by
    # nobody either (narrowing happens before the workflow is entered).
    assert recorded == []
    # The code-host lookup ran only where the run is in the bound repository.
    assert all(repo == verifier.calls[0][1] for _, repo in verifier.calls)


def test_the_lifespan_wires_the_agent_route_only_when_auth0_is_configured(
    url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        del engine
        configured = create_app(settings(url))
        async with configured.router.lifespan_context(configured):
            wired = isinstance(
                configured.state.agent_investigations, AgentInvestigations
            )
        plain = create_app(Settings(Secret(url)))
        async with plain.router.lifespan_context(plain):
            absent = getattr(plain.state, "agent_investigations", None) is None
        return wired, absent

    assert run(url, scenario) == (True, True)


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    return migrated_database_url
