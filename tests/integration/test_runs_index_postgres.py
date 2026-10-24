"""The runs index over real PostgreSQL: the real store query, real ingested
runs, and the member flow's real authorization chain.

The provider answer is canned (like every other Postgres integration test):
these are PostgreSQL integration tests with faked provider responses — NOT
live GitHub verification, and the runs listed here are real ingested CI
reports over real tables."""

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support import send_raw

from flakepilot.identity.application.iam import digest
from flakepilot.integrations.application.ports import CodeHostError, CodeHostRole
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.http import auth
from flakepilot.repository.application.verification import BoundCodeAccessVerifier
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.test_intelligence.application.run_view import RunViewer
from flakepilot.test_intelligence.application.store import TiStore
from flakepilot.test_intelligence.domain.results import HEADERS
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

OLD, NEW = "1" * 40, "2" * 40
COOKIE = auth.AuthConfig("https://fp.example.test", True).session_cookie


def fresh_pair() -> tuple[str, str, str]:
    """One (org, repo, token) per test: two tests sharing the module names
    would collide on real tables."""
    return str(uuid.uuid4()), str(uuid.uuid4()), "fpi_" + secrets.token_urlsafe(32)


def token_record(org: str, repo: str, token: str) -> dict[str, Any]:
    """Bind params for one ci_upload_tokens row (the SQL's own placeholders)."""
    return {
        "id": str(uuid.uuid4()),
        "org": org,
        "repo": repo,
        "h": digest(token),
        "p": token[4:12],
    }


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
        if result.returns_rows:
            return [tuple(row) for row in result.mappings()]
        await session.commit()
        return []


def report(*cases: tuple[str, str, str]) -> bytes:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{inner}</testcase>'
        for c, n, inner in cases
    )
    return f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>'.encode()


FAIL = "<failure>asserting</failure>"


def metadata(**overrides: str) -> list[tuple[str, str]]:
    values = {
        "ci_source": "github_actions",
        "ci_run_id": uuid.uuid4().hex,
        "ci_attempt": "1",
        "job_name": "unit tests",
        "commit_sha": NEW,
        "ref": "refs/heads/main",
        "environment": "ubuntu-24.04",
        "started_at": "2026-10-21T10:00:00+00:00",
        "finished_at": "2026-10-21T10:05:00+00:00",
        **overrides,
    }
    return [(HEADERS[k], v) for k, v in values.items()]


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    return migrated_database_url


@pytest.fixture(scope="module")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("artifacts")


class Client:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.bodies: list[bytes] = []

    async def upload(
        self, token: str, body: bytes, meta: list[tuple[str, str]]
    ) -> tuple[int, dict[str, Any]]:
        status, _, content = await send_raw(
            self.app,
            "POST",
            "/ingest/junit",
            [
                ("authorization", f"Bearer {token}"),
                ("content-type", "application/xml"),
                *meta,
            ],
            body,
        )
        self.bodies.append(content)
        return status, json.loads(content)


@asynccontextmanager
async def client(url: str, artifact_dir: Path) -> AsyncIterator[Client]:
    app = create_app(Settings(Secret(url), artifact_dir=artifact_dir))
    async with app.router.lifespan_context(app):
        yield Client(app)


class _UnusedIdentityProvider:
    """Satisfies create_app's factory signature; sign-in endpoints are never hit."""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        raise AssertionError("sign-in is not part of the runs-index test")

    async def identify(self, *, code: str, code_verifier: str) -> Any:
        raise AssertionError("sign-in is not part of the runs-index test")


def repo_directory(
    engine: AsyncEngine,
) -> Callable[[], AbstractAsyncContextManager[SqlRepoDirectory]]:
    @asynccontextmanager
    async def open() -> AsyncIterator[SqlRepoDirectory]:
        async with transaction(engine) as session:
            yield SqlRepoDirectory(session)

    return open


def test_the_real_store_lists_ingested_runs_newest_first_and_bounded(
    url: str, artifact_dir: Path
) -> None:
    """The operator read: real ingested runs, listed by the real query."""

    async def scenario(engine: AsyncEngine) -> list[str]:
        ORG, REPO, TOKEN = fresh_pair()
        record = token_record(ORG, REPO, TOKEN)
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'RunsIndex', now())",
            o=ORG,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:id, :org, :repo, :h, :p, :id, now(), NULL)",
            **record,
        )
        run_ids: list[str] = []
        async with client(url, artifact_dir) as api:
            for index, commit in enumerate((OLD, NEW)):
                status, content = await api.upload(
                    TOKEN,
                    report(("ri", "one-test", FAIL if index == 0 else "")),
                    metadata(commit_sha=commit, ci_run_id=f"ri-{index}"),
                )
                assert status == 201, content
                run_ids.append(content["run_id"])

        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[TiStore]:
            async with transaction(engine) as session:
                yield SqlTiStore(session)

        listed = await RunViewer(unit_of_work).runs_index(org_id=ORG, repo_id=REPO)
        # Newest first: the second upload's run is listed first.
        assert [entry.run_id for entry in listed.entries] == [
            run_ids[1],
            run_ids[0],
        ], listed.entries
        first = listed.entries[0]
        assert (first.source, first.commit_sha, first.revision_status) == (
            "ci_upload",
            NEW,
            "unchecked",
        )
        assert (first.summary.tests, first.summary.passed) == (1, 1)

        bounded = await RunViewer(unit_of_work).runs_index(
            org_id=ORG, repo_id=REPO, limit=1
        )
        assert [entry.run_id for entry in bounded.entries] == [run_ids[1]]
        # The technical default page is also exact: two runs, two rows.
        for limit in (0, -3):  # clamped to 1, never a full table
            clamped = await RunViewer(unit_of_work).runs_index(
                org_id=ORG, repo_id=REPO, limit=limit
            )
            assert len(clamped.entries) == 1
        # A foreign org's listing is simply empty.
        elsewhere = await RunViewer(unit_of_work).runs_index(
            org_id=str(uuid.uuid4()), repo_id=REPO
        )
        assert elsewhere.entries == ()
        # And the same listing works for an empty pair.
        nothing = await RunViewer(unit_of_work).runs_index(
            org_id=ORG, repo_id=str(uuid.uuid4())
        )
        assert nothing.entries == ()
        return run_ids

    run(url, scenario)


def test_the_runs_index_authorizes_a_signed_in_member_over_real_tables(
    url: str, artifact_dir: Path
) -> None:
    """The HTTP listing chain end to end: real sessions, memberships, grants,
    binding, and verifier; only the provider answer is canned.

    One verified member sees the listing with one real ingested run and a
    link to its view; a member without `repo.view`, a stranger's session, an
    unavailable provider, and the deployment's default (no credentials, fail
    closed) all learn nothing (404/404/401/404/404).
    """

    async def scenario(engine: AsyncEngine) -> None:
        ORG, REPO, TOKEN = fresh_pair()
        record = token_record(ORG, REPO, TOKEN)
        viewer, no_grant, outsider = (str(uuid.uuid4()) for _ in range(3))
        ok_session, grantless_session, stray_session = (
            secrets.token_urlsafe(32) for _ in range(3)
        )
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'RunsIndexHttp', now())",
            o=ORG,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:id, :org, :repo, :h, :p, :id, now(), NULL)",
            **record,
        )
        for user, subject in ((viewer, "ri-v"), (no_grant, "ri-n"), (outsider, "ri-o")):
            await sql(
                engine,
                "INSERT INTO iam.users (id, idp_issuer, idp_subject, github_login, "
                "created_at) VALUES (:u, 'https://github.test', :s, :s, now())",
                u=user,
                s=subject,
            )
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
            (outsider, stray_session),
        ):
            await sql(
                engine,
                "INSERT INTO iam.sessions (id_hash, user_id, created_at, "
                "last_seen_at, expires_at) VALUES (:h, :u, now(), now(), "
                "now() + interval '1 hour')",
                h=digest(session_id),
                u=user,
            )
        async with client(url, artifact_dir) as api:
            status, content = await api.upload(
                TOKEN,
                report(("ri", "listed-test", FAIL)),
                metadata(commit_sha=NEW, ci_run_id="ri-http-0"),
            )
            assert status == 201, content
            run_id = content["run_id"]

        class InstalledRole:
            async def repository_permission(
                self, *, owner: str, name: str, login: str
            ) -> CodeHostRole:
                assert (owner, name) == ("acme", "web")  # the binding's identity
                return CodeHostRole.READ

        class UnreachableProvider:
            async def repository_permission(self, **kw: Any) -> CodeHostRole:
                raise CodeHostError("access_unverified", unavailable=True)

        def app_with(provider: Any) -> FastAPI:
            def verifier_factory(engine: AsyncEngine, settings: Settings) -> Any:
                return BoundCodeAccessVerifier(repo_directory(engine), provider)

            return create_app(
                Settings(
                    Secret(url),
                    artifact_dir=artifact_dir,
                    public_origin="https://flakepilot-index.test",
                ),
                identity_provider_factory=lambda _: _UnusedIdentityProvider(),
                access_verifier=verifier_factory,
            )

        async def fetch(
            target_path: str, session_id: str | None, provider: Any = None
        ) -> tuple[int, str]:
            app = app_with(provider if provider is not None else InstalledRole())
            headers = (
                [] if session_id is None else [("cookie", f"{COOKIE}={session_id}")]
            )
            async with app.router.lifespan_context(app):
                got, _, body = await send_raw(app, "GET", target_path, headers)
            return got, body.decode()

        path = f"/orgs/{ORG}/repos/{REPO}/runs"

        # Verified code-host read: the listing is served with the real run.
        granted, page = await fetch(path, ok_session)
        assert granted == 200, page
        assert f'href="/orgs/{ORG}/runs/{run_id}"' in page
        assert "newest first" in page and "unchecked" in page

        # A bigger limit or a smaller one is a page size, nothing else.
        (small_status, _) = await fetch(path + "?limit=1", ok_session)
        assert small_status == 200

        # A verified code-host NONE is a fact: the same non-disclosing denial.
        none_status, none_body = await fetch(
            path, ok_session, provider=InstalledRoleNone()
        )
        assert none_status == 404 and '"not_found"' in none_body

        # An unavailable provider is not evidence either: denied, nothing listed.
        down_status, down_body = await fetch(path, ok_session, UnreachableProvider())
        assert down_status == 404 and '"not_found"' in down_body

        # The grant-less member, a stranger, and no session learn nothing.
        grantless, _ = await fetch(path, grantless_session)
        stranger, _ = await fetch(path, stray_session)
        anonymous, _ = await fetch(path, None)
        assert (grantless, stranger, anonymous) == (404, 404, 401)
        assert (await fetch(path, "not-a-session"))[0] == 401

        # Production default: no GitHub App credentials configured, so the real
        # verifier's provider can mint no token and answers unavailable.
        default_app = create_app(
            Settings(
                Secret(url),
                artifact_dir=artifact_dir,
                public_origin="https://flakepilot-index.test",
            ),
            identity_provider_factory=lambda _: _UnusedIdentityProvider(),
        )
        headers = [("cookie", f"{COOKIE}={ok_session}")]
        async with default_app.router.lifespan_context(default_app):
            got, _, body = await send_raw(default_app, "GET", path, headers)
        assert (got, '"not_found"' in body.decode()) == (404, True)

    run(url, scenario)


class InstalledRoleNone:
    async def repository_permission(
        self, *, owner: str, name: str, login: str
    ) -> CodeHostRole:
        return CodeHostRole.NONE
