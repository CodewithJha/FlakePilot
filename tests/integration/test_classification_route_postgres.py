"""The classification JSON route over real PostgreSQL (`ti-classification/1`
verdicts as data, goal B9).

The real authorization chain with real sessions, memberships, grants, and
verifier — only the provider answer is canned (like every other Postgres
integration test). One verified member gets the run's verdicts as a JSON
document; a member without `repo.view`, a stranger, an unavailable provider,
and no session all learn nothing. These are real ingested CI reports over real
tables, not provider verification.
"""

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support import send_raw

from flakepilot.identity.application.iam import CiUploadPrincipal, digest
from flakepilot.integrations.application.ports import CodeHostError, CodeHostRole
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.http import auth
from flakepilot.repository.application.verification import BoundCodeAccessVerifier
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.ingestion import Ingestion
from flakepilot.test_intelligence.domain.results import HEADERS
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

FAIL = "<failure>assertion</failure>"
COOKIE = auth.AuthConfig("https://fp.example.test", True).session_cookie


@pytest.fixture(scope="module")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("artifacts")


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
        result: Any = await session.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


def _report(*cases: tuple[str, str, str]) -> bytes:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{inner}</testcase>'
        for c, n, inner in cases
    )
    return (
        f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}'
        "</testsuite></testsuites>".encode()
    )


def _metadata() -> dict[str, list[str]]:
    values = {
        "ci_source": "github_actions",
        "ci_run_id": uuid.uuid4().hex,
        "ci_attempt": "1",
        "job_name": "unit tests",
        "commit_sha": "1" * 40,
        "ref": "refs/heads/main",
        "environment": "ubuntu-24.04",
        "started_at": "2026-10-21T10:00:00+00:00",
        "finished_at": "2026-10-21T10:05:00+00:00",
    }
    return {HEADERS[k]: [v] for k, v in values.items()}


class _UnusedIdentityProvider:
    """Satisfies create_app's factory signature; sign-in endpoints are never hit."""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        raise AssertionError("sign-in is not part of this test")

    async def identify(self, *, code: str, code_verifier: str) -> Any:
        raise AssertionError("sign-in is not part of this test")


def test_the_classification_json_route_serves_verdicts_and_fails_closed(
    migrated_database_url: str, artifact_dir: Path
) -> None:
    """One verified member gets the verdicts as data; every denial is the
    same non-disclosing 404 (grant-less member, unavailable provider,
    stranger's session, no session)."""

    async def scenario(engine: AsyncEngine) -> None:
        ORG, REPO, TOKEN = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        raw_token = "fpi_" + secrets.token_urlsafe(32)
        viewer, no_grant, stranger = (str(uuid.uuid4()) for _ in range(3))
        ok_session, grantless_session, stranger_session = (
            secrets.token_urlsafe(32) for _ in range(3)
        )
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'B9', now())",
            o=ORG,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:t, :o, :r, :h, '12345678', :t, now(), NULL)",
            t=TOKEN,
            o=ORG,
            r=REPO,
            h=digest(raw_token),
        )
        for user, subject in ((viewer, "b9-v"), (no_grant, "b9-n"), (stranger, "b9-s")):
            await sql(
                engine,
                "INSERT INTO iam.users (id, idp_issuer, idp_subject, github_login, "
                "created_at) VALUES (:u, 'https://github.test', :s, :s, now())",
                u=user,
                s=subject,
            )
            if user is not stranger:
                await sql(
                    engine,
                    "INSERT INTO iam.memberships (org_id, user_id, org_admin, created_at) "
                    "VALUES (:o, :u, FALSE, now())",
                    o=ORG,
                    u=user,
                )
        await sql(
            engine,
            'INSERT INTO iam.repo_grants (org_id, user_id, repo_id, "grant", '
            "granted_by, created_at) VALUES (:o, :u, :r, 'repo.view', :u, now())",
            o=ORG,
            u=viewer,
            r=REPO,
        )
        await sql(
            engine,
            "INSERT INTO repo.repositories (id, org_id, host, owner, name, created_at) "
            "VALUES (:r, :o, 'github', 'acme', 'web', now())",
            r=REPO,
            o=ORG,
        )
        for user, session_id in (
            (viewer, ok_session),
            (no_grant, grantless_session),
            (stranger, stranger_session),
        ):
            await sql(
                engine,
                "INSERT INTO iam.sessions (id_hash, user_id, created_at, "
                "last_seen_at, expires_at) VALUES (:h, :u, now(), now(), "
                "now() + interval '1 hour')",
                h=digest(session_id),
                u=user,
            )

        @asynccontextmanager
        async def unit() -> AsyncIterator[SqlTiStore]:
            async with transaction(engine) as session:
                yield SqlTiStore(session)

        service = Ingestion(unit, LocalFsArtifactStore(artifact_dir), JUnitParser())
        principal = CiUploadPrincipal(TOKEN, ORG, REPO)
        ingested = await service.ingest(
            principal,
            headers=_metadata(),
            content_type="application/xml",
            content_encoding=None,
            body=_report(("pkg.Suite", "fails", FAIL), ("pkg.Suite", "passes", "")),
            origin=Origin(Channel.HTTP, "b9"),
        )
        run_id = ingested.run_id

        class InstalledRole:
            async def repository_permission(
                self, *, owner: str, name: str, login: str
            ) -> CodeHostRole:
                assert (owner, name) == ("acme", "web")
                return CodeHostRole.READ

        class UnreachableProvider:
            async def repository_permission(self, **kw: Any) -> CodeHostRole:
                raise CodeHostError("access_unverified", unavailable=True)

        def app_with(provider: Any) -> FastAPI:
            def verifier_factory(engine: AsyncEngine, settings: Settings) -> Any:
                @asynccontextmanager
                async def directories() -> AsyncIterator[SqlRepoDirectory]:
                    async with transaction(engine) as session:
                        yield SqlRepoDirectory(session)

                return BoundCodeAccessVerifier(directories, provider)

            return create_app(
                Settings(
                    Secret(migrated_database_url),
                    artifact_dir=artifact_dir,
                    public_origin="https://flakepilot-b9.test",
                ),
                identity_provider_factory=lambda _: _UnusedIdentityProvider(),
                access_verifier=verifier_factory,
            )

        path = f"/orgs/{ORG}/runs/{run_id}/classification"

        async def fetch(
            target: str, session_id: str | None, provider: Any = None
        ) -> tuple[int, dict[str, Any]]:
            app = app_with(provider if provider is not None else InstalledRole())
            headers = (
                [] if session_id is None else [("cookie", f"{COOKIE}={session_id}")]
            )
            async with app.router.lifespan_context(app):
                got, _, body = await send_raw(app, "GET", target, headers)
            return got, json.loads(body)

        # Verified read: the verdicts as data, exactly the rule set's output.
        status, payload = await fetch(path, ok_session)
        assert status == 200, payload
        assert payload["schema"] == "flakepilot.classification/1"
        assert payload["run_id"] == run_id and payload["repo_id"] == REPO
        by_name = {case["name"]: case for case in payload["cases"]}
        # One run of history is not enough for any verdict: both cases are
        # `inconclusive` / `insufficient_history` — exactly what
        # ti-classification/1 answers from this evidence.
        for name in ("passes", "fails"):
            verdict = by_name[name]["classification"]
            assert verdict["failure_class"] == "inconclusive"
            assert verdict["reason"] == "insufficient_history"
            assert verdict["rule_version"] == "ti-classification/1"
            assert verdict["evidence"] is not None

        # Every denial is the same non-disclosing 404; no session is 401.
        grantless, _ = await fetch(path, grantless_session)
        stranger_status, _ = await fetch(path, stranger_session)
        down, down_body = await fetch(path, ok_session, UnreachableProvider())
        anonymous, _ = await fetch(path, None)
        assert (grantless, stranger_status, anonymous) == (404, 404, 401)
        assert down == 404 and '"not_found"' in json.dumps(down_body)

    run(migrated_database_url, scenario)
