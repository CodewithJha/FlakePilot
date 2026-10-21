"""An authorized session can record a closed reproduction. Nothing is executed."""

import asyncio
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support import send_raw

from flakepilot.identity.application.authorization import (
    OrgAccess,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import (
    SESSION_LIFETIME,
    Iam,
    digest,
)
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.application.ports import VerifiedIdentity
from flakepilot.interface.composition_root import (
    GitHubApp,
    Secret,
    Settings,
    create_app,
    iam_unit_of_work,
)
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.reproduction import request_reproduction
from flakepilot.repository.application.source import new_binding
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Origin

pytestmark = pytest.mark.integration

ORIGIN = "https://fp.example"
SHA = "ab" * 20
SELECTOR = "tests.login.TestLogin.test_ok"
IMAGE = "ghcr.io/example/reproduce@sha256:" + ("cd" * 32)


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class _Idle:
    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        del state, code_challenge
        return "https://github.com/login/oauth/authorize"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        del code, code_verifier
        raise AssertionError("sign-in was called")


class _Host:
    def __init__(self) -> None:
        self.calls = 0

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        del owner, name
        self.calls += 1
        return claimed_sha

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        del owner, name, sha
        raise AssertionError("archive fetch")


def test_closed_mode_records_one_row_and_does_not_fetch_or_enqueue(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(settings: object) -> None:
        del settings
        raise AssertionError("code host")

    monkeypatch.setattr(
        "flakepilot.interface.composition_root.reproduction._host", refuse
    )

    async def scenario(engine: AsyncEngine) -> tuple[int, str, int, int, int]:
        iam = Iam(iam_unit_of_work(engine), None)
        org_id = await iam.bootstrap_org(
            name=f"ops-{uuid.uuid4().hex[:8]}",
            admin_issuer="https://github.com",
            admin_subject="4242",
            admin_login="ada",
            origin=Origin(Channel.CLI, "bootstrap-operator"),
        )
        async with transaction(engine) as session:
            user_id = (
                await session.execute(
                    text(
                        "SELECT user_id::text FROM iam.memberships WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).scalar_one()
            binding = new_binding(
                org_id=org_id, owner="acme", name=f"web-{uuid.uuid4().hex[:8]}"
            )
            await SqlRepoDirectory(session).add(binding, now=datetime.now(UTC))
            session_id = secrets.token_urlsafe(32)
            await SqlIamStore(session).create_session(
                id_hash=digest(session_id),
                user_id=user_id,
                lifetime=SESSION_LIFETIME,
            )
        settings = Settings(
            Secret(migrated_database_url),
            public_origin=ORIGIN,
            github_app=GitHubApp("Iv1.test", Secret("unused-client-secret")),
        )
        app = create_app(settings, identity_provider_factory=lambda _: _Idle())
        cookie = ("cookie", f"__Host-flakepilot_session={session_id}")
        origin = ("origin", ORIGIN)
        body = json.dumps(
            {"repo_id": binding.repo_id, "selector": SELECTOR, "sha": SHA}
        ).encode()
        target = f"/orgs/{org_id}/reproductions"

        async def call(
            method: str,
            path: str,
            payload: bytes = b"",
            headers: list[tuple[str, str]] | None = None,
        ) -> tuple[int, dict[str, object]]:
            status, _, content = await send_raw(app, method, path, headers, payload)
            return status, json.loads(content)

        async with app.router.lifespan_context(app):
            missing, _ = await call("POST", target, body)
            denied, _ = await call("POST", target, body, [cookie])
            extra, extra_body = await call(
                "POST",
                target,
                json.dumps(
                    {
                        "repo_id": binding.repo_id,
                        "selector": SELECTOR,
                        "sha": SHA,
                        "org_id": org_id,
                    }
                ).encode(),
                [cookie, origin, ("content-type", "application/json")],
            )
            created, first = await call(
                "POST",
                target,
                body,
                [cookie, origin, ("content-type", "application/json")],
            )
            again, second = await call(
                "POST",
                target,
                body,
                [cookie, origin, ("content-type", "application/json")],
            )
            status, viewed = await call(
                "GET",
                f"{target}/{first['id']}",
                headers=[cookie],
            )
            other, _ = await call(
                "GET",
                f"/orgs/{uuid.uuid4()}/reproductions/{first['id']}",
                headers=[cookie],
            )
        async with transaction(engine) as session:
            job_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM orch.jobs "
                            "WHERE payload->>'execution_id' = :id"
                        ),
                        {"id": first["id"]},
                    )
                ).scalar_one()
            )
            with pytest.raises(ResourceNotFound):
                await request_reproduction(
                    engine,
                    settings,
                    access=OrgAccess(str(uuid.uuid4()), org_id),
                    org_id=org_id,
                    repo_id=binding.repo_id,
                    selector=SELECTOR,
                    sha=SHA,
                    actor=Actor(ActorType.USER, user_id),
                    origin=Origin(Channel.HTTP, "corr-denied"),
                    now=datetime.now(UTC),
                )
        assert (missing, denied, extra) == (401, 403, 422)
        assert "org_id" not in json.dumps(extra_body)
        assert created == 201
        assert first["reason"] == "execution_closed"
        assert first["outcome"] == "inconclusive"
        assert first["evidence_status"] == "ineligible"
        assert first["evidence_reason"] == "execution_closed"
        assert first["artifacts"] == []
        assert again == 200 and second["id"] == first["id"]
        assert status == 200 and viewed["id"] == first["id"]
        assert "data" not in json.dumps(viewed["artifacts"])
        assert other == 404
        assert SHA not in json.dumps(extra_body)
        return job_count, str(first["state"]), created, again, other

    jobs, state, created, again, other = run(migrated_database_url, scenario)
    assert jobs == 0
    assert state == "completed"
    assert (created, again, other) == (201, 200, 404)


def test_evaluation_enqueues_an_id_and_does_not_fetch_an_archive(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    monkeypatch.setattr(
        "flakepilot.interface.composition_root.reproduction._host",
        lambda settings: host,
    )

    async def scenario(engine: AsyncEngine) -> tuple[int, str, bool, str]:
        iam = Iam(iam_unit_of_work(engine), None)
        org_id = await iam.bootstrap_org(
            name=f"eval-{uuid.uuid4().hex[:8]}",
            admin_issuer="https://github.com",
            admin_subject="5252",
            admin_login="bea",
            origin=Origin(Channel.CLI, "bootstrap-eval"),
        )
        async with transaction(engine) as session:
            user_id = (
                await session.execute(
                    text(
                        "SELECT user_id::text FROM iam.memberships WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).scalar_one()
            binding = new_binding(
                org_id=org_id, owner="acme", name=f"web-{uuid.uuid4().hex[:8]}"
            )
            await SqlRepoDirectory(session).add(binding, now=datetime.now(UTC))
        settings = Settings(
            Secret(migrated_database_url),
            execution_mode="evaluation",
            execution_image=IMAGE,
        )
        record, created = await request_reproduction(
            engine,
            settings,
            access=OrgAccess(user_id, org_id, org_admin=True),
            org_id=org_id,
            repo_id=binding.repo_id,
            selector="tests.login.TestLogin.test_eval",
            sha=SHA,
            actor=Actor(ActorType.USER, user_id),
            origin=Origin(Channel.HTTP, "corr-eval-1"),
            now=datetime.now(UTC),
        )
        async with transaction(engine) as session:
            payload = await session.execute(
                text(
                    "SELECT payload::text FROM orch.jobs "
                    "WHERE payload->>'execution_id' = :id"
                ),
                {"id": record.id},
            )
        return host.calls, str(payload.scalar_one()), created, record.id

    calls, payload, created, execution_id = run(migrated_database_url, scenario)
    assert calls == 1
    assert created is True
    assert json.loads(payload) == {"execution_id": execution_id}
