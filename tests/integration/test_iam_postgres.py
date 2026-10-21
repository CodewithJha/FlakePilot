"""IAM persistence, sessions, CI upload tokens, sign-in over HTTP, and the encrypted secret
store against real PostgreSQL (one migrated throwaway database for this module)."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import Depends
from sqlalchemy import exc, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine
from support import RawResponse, send_raw

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
    authorize,
)
from flakepilot.identity.application.iam import (
    CiUploadPrincipal,
    Iam,
    IamStore,
    SessionPrincipal,
    digest,
)
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.application.ports import SecretScope, VerifiedIdentity
from flakepilot.integrations.infrastructure.secret_store import (
    EncryptedSecretStore,
    SecretStoreError,
)
from flakepilot.interface.composition_root import (
    GitHubApp,
    Secret,
    Settings,
    create_app,
)
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.http.auth import ci_upload_principal, org_access

pytestmark = pytest.mark.integration

ISSUER = "https://github.com"
ORIGIN = "https://fp.example"
CLIENT_SECRET = "client-secret-SENTINEL-61"
ORG_A, ORG_B = str(uuid.uuid4()), str(uuid.uuid4())
REPO_1, REPO_2, REPO_3 = (str(uuid.uuid4()) for _ in range(3))
USER_A, USER_B = str(uuid.uuid4()), str(uuid.uuid4())
DUPLICATE_HASH = os.urandom(32)
READ = CodeHostAccess.READ


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


class FakeGitHub:
    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://github.test/authorize?state={state}"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        return VerifiedIdentity(ISSUER, "1001", "octo-renamed")


def iam(engine: AsyncEngine) -> Iam:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[IamStore]:
        async with transaction(engine) as session:
            yield SqlIamStore(session)

    return Iam(unit_of_work, FakeGitHub())


async def sign_in(service: Iam, previous: str | None = None) -> str:
    return await service.complete_sign_in(
        expected_state="s",
        received_state="s",
        code="c",
        code_verifier="v",
        error=None,
        previous_session=previous,
    )


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    async def seed(engine: AsyncEngine) -> None:
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) "
            "VALUES (:a, 'Org A', now()), (:b, 'Org B', now())",
            a=ORG_A,
            b=ORG_B,
        )
        await sql(
            engine,
            "INSERT INTO iam.users (id, idp_issuer, idp_subject, github_login, created_at) "
            "VALUES (:ua, :iss, '1001', 'octo', now()), (:ub, :iss, '2002', 'b', now())",
            ua=USER_A,
            ub=USER_B,
            iss=ISSUER,
        )
        await sql(
            engine,
            "INSERT INTO iam.memberships (org_id, user_id, created_at) "
            "VALUES (:a, :ua, now()), (:b, :ub, now())",
            a=ORG_A,
            b=ORG_B,
            ua=USER_A,
            ub=USER_B,
        )
        await sql(
            engine,
            'INSERT INTO iam.repo_grants (org_id, user_id, repo_id, "grant", '
            "granted_by, created_at) VALUES "
            "(:a, :ua, :r1, 'repo.configure', :ua, now()), "
            "(:a, :ua, :r2, 'repo.view', :ua, now()), "
            "(:b, :ub, :r1, 'repo.configure', :ub, now())",
            a=ORG_A,
            b=ORG_B,
            ua=USER_A,
            ub=USER_B,
            r1=REPO_1,
            r2=REPO_2,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at) "
            "VALUES (:id, :a, :r1, :h, 'abcdefgh', :ua, now())",
            id=str(uuid.uuid4()),
            a=ORG_A,
            r1=REPO_1,
            h=DUPLICATE_HASH,
            ua=USER_A,
        )

    run(migrated_database_url, seed)
    return migrated_database_url


SESSION = (
    "INSERT INTO iam.sessions (id_hash, user_id, created_at, last_seen_at, expires_at, "
    "revoked_at, revoke_reason) VALUES (:h, :ua, now(), now(), now(), {})"
)


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO iam.users (id, idp_issuer, idp_subject, github_login, created_at) "
            "VALUES (:id, 'https://github.com', '1001', 'dup', now())"
        ),
        (
            'INSERT INTO iam.repo_grants (org_id, user_id, repo_id, "grant", granted_by, '
            "created_at) VALUES (:a, :ub, :r1, 'repo.view', :ua, now())"
        ),
        (
            'INSERT INTO iam.repo_grants (org_id, user_id, repo_id, "grant", granted_by, '
            "created_at) VALUES (:a, :ua, :r3, 'repo.admin', :ua, now())"
        ),
        SESSION.format("now(), NULL"),
        SESSION.format("NULL, 'rotated'"),
        SESSION.format("now(), 'expired'"),
        SESSION.replace(":h", ":short").format("NULL, NULL"),
        (
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at) "
            "VALUES (:id, :a, :r1, :h, 'abc', :ua, now())"
        ),
        (
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at) "
            "VALUES (:id, :a, :r1, :dup, 'abcdefgh', :ua, now())"
        ),
    ],
    ids=[
        "duplicate-identity",
        "grant-for-member-of-another-org",
        "unknown-grant",
        "revoked-without-reason",
        "reason-without-revocation",
        "unknown-reason",
        "short-session-hash",
        "short-display-prefix",
        "duplicate-token-hash",
    ],
)
def test_schema_rejects_invalid_rows(url: str, statement: str) -> None:
    params = {
        "id": str(uuid.uuid4()),
        "a": ORG_A,
        "ua": USER_A,
        "ub": USER_B,
        "r1": REPO_1,
        "r3": REPO_3,
        "h": os.urandom(32),
        "short": os.urandom(31),
        "dup": DUPLICATE_HASH,
    }
    used = {k: v for k, v in params.items() if f":{k}" in statement}

    async def insert(engine: AsyncEngine) -> None:
        with pytest.raises(exc.IntegrityError):
            await sql(engine, statement, **used)

    run(url, insert)


def test_session_lifecycle_rotation_and_sign_out(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> list[Any]:
        service = iam(engine)
        first = await sign_in(service)
        assert await service.authenticate(first) == SessionPrincipal(USER_A)
        second = await sign_in(service, previous=first)
        assert await service.authenticate(first) is None
        assert await service.authenticate(second) == SessionPrincipal(USER_A)
        await service.sign_out(second)
        assert await service.authenticate(second) is None
        rows = await sql(
            engine,
            "SELECT id_hash, revoke_reason FROM iam.sessions WHERE id_hash IN (:f, :s)",
            f=digest(first),
            s=digest(second),
        )
        [login] = await sql(
            engine, "SELECT github_login FROM iam.users WHERE id = :u", u=USER_A
        )
        return [{bytes(h): reason for h, reason in rows}, login[0], first, second]

    reasons, login, first, second = run(url, scenario)
    assert reasons == {digest(first): "rotated", digest(second): "signed_out"}
    assert login == "octo-renamed"


@pytest.mark.parametrize(
    "expire",
    [
        "expires_at = now() - interval '1 second'",
        "last_seen_at = now() - interval '2 hours 1 minute'",
    ],
    ids=["absolute", "idle"],
)
def test_expired_sessions_are_rejected(url: str, expire: str) -> None:
    async def scenario(engine: AsyncEngine) -> SessionPrincipal | None:
        service = iam(engine)
        session_id = await sign_in(service)
        await sql(
            engine,
            f"UPDATE iam.sessions SET {expire} WHERE id_hash = :h",
            h=digest(session_id),
        )
        return await service.authenticate(session_id)

    assert run(url, scenario) is None


def test_last_seen_moves_at_most_every_five_minutes_even_concurrently(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[bool, bool, list[bool]]:
        service = iam(engine)
        session_id = await sign_in(service)
        h = digest(session_id)

        async def age(minutes: int) -> Any:
            await sql(
                engine,
                "UPDATE iam.sessions SET last_seen_at = now() - make_interval(mins => :m) "
                "WHERE id_hash = :h",
                m=minutes,
                h=h,
            )
            [[seen]] = await sql(
                engine, "SELECT last_seen_at FROM iam.sessions WHERE id_hash = :h", h=h
            )
            return seen

        async def last_seen() -> Any:
            [[seen]] = await sql(
                engine, "SELECT last_seen_at FROM iam.sessions WHERE id_hash = :h", h=h
            )
            return seen

        before = await age(4)
        await service.authenticate(session_id)
        unchanged = await last_seen() == before
        before = await age(6)
        results = await asyncio.gather(
            *(service.authenticate(session_id) for _ in range(10))
        )
        moved = await last_seen() > before
        return unchanged, moved, [r == SessionPrincipal(USER_A) for r in results]

    unchanged, moved, authenticated = run(url, scenario)
    assert unchanged and moved
    assert all(authenticated)


def test_org_access_comes_only_from_the_org_records(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> OrgAccess:
        service = iam(engine)
        with pytest.raises(ResourceNotFound):
            await service.org_access(SessionPrincipal(USER_A), ORG_B)
        with pytest.raises(ResourceNotFound):
            await service.org_access(SessionPrincipal(str(uuid.uuid4())), ORG_A)
        return await service.org_access(SessionPrincipal(USER_A), ORG_A)

    access = run(url, scenario)
    assert access == OrgAccess(
        USER_A,
        ORG_A,
        False,
        frozenset({(REPO_1, Grant.CONFIGURE), (REPO_2, Grant.VIEW)}),
    )
    with pytest.raises(ResourceNotFound):  # same repo ID in org B is not reachable
        authorize(access, Grant.VIEW, org_id=ORG_B, repo_id=REPO_1, code_host=READ)


def test_ci_upload_tokens_are_scoped_hashed_and_revocable(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        service = iam(engine)
        access_a = await service.org_access(SessionPrincipal(USER_A), ORG_A)
        access_b = await service.org_access(SessionPrincipal(USER_B), ORG_B)
        token = await service.create_ci_upload_token(
            access_a, org_id=ORG_A, repo_id=REPO_1, code_host=READ
        )
        token_b = await service.create_ci_upload_token(
            access_b, org_id=ORG_B, repo_id=REPO_1, code_host=READ
        )
        principal = await service.verify_ci_upload_token(token)
        assert principal is not None
        assert (principal.org_id, principal.repo_id) == (ORG_A, REPO_1)
        principal_b = await service.verify_ci_upload_token(token_b)
        assert principal_b is not None and principal_b.org_id == ORG_B

        [row] = await sql(
            engine,
            "SELECT * FROM iam.ci_upload_tokens WHERE id = :id",
            id=principal.token_id,
        )
        assert row.token_hash == digest(token) and row.display_prefix == token[4:12]
        assert token not in repr(tuple(row))

        with pytest.raises(PermissionDenied):  # view only on REPO_2
            await service.create_ci_upload_token(
                access_a, org_id=ORG_A, repo_id=REPO_2, code_host=READ
            )
        revoke = service.revoke_ci_upload_token
        with pytest.raises(ResourceNotFound):  # another org cannot even see it
            await revoke(
                access_b,
                org_id=ORG_A,
                repo_id=REPO_1,
                token_id=principal.token_id,
                code_host=READ,
            )
        with pytest.raises(ResourceNotFound):  # token is not in REPO_3
            await revoke(
                access_a,
                org_id=ORG_A,
                repo_id=REPO_3,
                token_id=principal.token_id,
                code_host=READ,
            )
        await revoke(
            access_a,
            org_id=ORG_A,
            repo_id=REPO_1,
            token_id=principal.token_id,
            code_host=READ,
        )
        assert await service.verify_ci_upload_token(token) is None
        with pytest.raises(ResourceNotFound):
            await revoke(
                access_a,
                org_id=ORG_A,
                repo_id=REPO_1,
                token_id=principal.token_id,
                code_host=READ,
            )

    run(url, scenario)


def test_secret_store_round_trip_and_tenant_binding(url: str) -> None:
    key, value = os.urandom(32), "plaintext-SENTINEL-73"

    async def scenario(engine: AsyncEngine) -> None:
        store = EncryptedSecretStore(lambda: transaction(engine), key)
        org_a, org_b = SecretScope(ORG_A), SecretScope(ORG_B)
        reference = await store.put(org_a, "token", value)
        assert await store.resolve(org_a, "token") == value
        assert await store.put(org_a, "token", value + "-2") == reference
        assert await store.resolve(org_a, "token") == value + "-2"
        await store.put(SecretScope(), "token", "instance")
        assert await store.resolve(SecretScope(), "token") == "instance"
        [[ciphertext]] = await sql(
            engine,
            "SELECT ciphertext FROM int_secrets.secrets WHERE id = :id",
            id=reference,
        )
        assert value.encode() not in bytes(ciphertext)
        with pytest.raises(SecretStoreError):
            await EncryptedSecretStore(
                lambda: transaction(engine), os.urandom(32)
            ).resolve(org_a, "token")
        await sql(  # a ciphertext moved to another org must not decrypt there
            engine,
            "UPDATE int_secrets.secrets SET org_id = :b WHERE id = :id",
            b=ORG_B,
            id=reference,
        )
        with pytest.raises(SecretStoreError):
            await store.resolve(org_b, "token")
        with pytest.raises(SecretStoreError):
            await store.resolve(org_a, "token")
        await store.delete(org_b, "token")
        assert (
            await sql(
                engine, "SELECT 1 FROM int_secrets.secrets WHERE id = :id", id=reference
            )
            == []
        )

    run(url, scenario)


def bootstrap_org(url: str, *argv: str) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    return subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root", "bootstrap-org"]
        + list(argv),
        env={**environ, "FLAKEPILOT_DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_bootstrap_org_creates_an_org_admin_once(url: str) -> None:
    name = f"Bootstrap {uuid.uuid4().hex[:8]}"
    created = bootstrap_org(
        url, "--name", name, "--admin-github-id", "7007", "--admin-login", "boot-admin"
    )
    assert created.returncode == 0, created.stderr
    org_id = created.stdout.strip()
    assert str(uuid.UUID(org_id)) == org_id and created.stderr == ""

    again = bootstrap_org(url, "--name", name, "--admin-github-id", "7007")
    assert again.returncode == 1 and again.stdout == ""
    assert again.stderr == "bootstrap-org: an org with this name already exists\n"

    async def login_of_1001(engine: AsyncEngine) -> str:
        [[login]] = await sql(
            engine, "SELECT github_login FROM iam.users WHERE idp_subject = '1001'"
        )
        return str(login)

    before = run(url, login_of_1001)
    existing = bootstrap_org(url, "--name", f"{name} 2", "--admin-github-id", "1001")
    assert existing.returncode == 0, existing.stderr

    async def scenario(engine: AsyncEngine) -> tuple[OrgAccess, int, list[Any]]:
        [[user_id]] = await sql(
            engine,
            "SELECT id FROM iam.users WHERE idp_issuer = :iss AND idp_subject = '7007'",
            iss=ISSUER,
        )
        access = await iam(engine).org_access(SessionPrincipal(str(user_id)), org_id)
        [[orgs]] = await sql(
            engine, "SELECT count(*) FROM iam.orgs WHERE name = :n", n=name
        )
        logins = await sql(
            engine,
            "SELECT idp_subject, github_login FROM iam.users "
            "WHERE idp_subject IN ('7007', '1001') ORDER BY idp_subject",
        )
        return access, orgs, [tuple(row) for row in logins]

    access, orgs, logins = run(url, scenario)
    assert access.org_admin and access.org_id == org_id and access.grants == frozenset()
    assert orgs == 1
    # An existing user keeps their login when none is given.
    assert logins == [("1001", before), ("7007", "boot-admin")]
    for result in (created, again, existing):
        output = result.stdout + result.stderr
        assert url not in output and "postgresql" not in output


def set_cookie(headers: list[tuple[str, str]], name: str) -> str:
    return next(v for k, v in headers if k == "set-cookie" and v.startswith(f"{name}="))


def value_of(cookie: str) -> str:
    return cookie.split(";", 1)[0].split("=", 1)[1]


def test_sign_in_sessions_csrf_and_protected_routes_over_http(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        Secret(url),
        public_origin=ORIGIN,
        github_app=GitHubApp("Iv1.test", Secret(CLIENT_SECRET)),
    )
    app = create_app(settings, identity_provider_factory=lambda _: FakeGitHub())

    @app.get("/test-only/orgs/{org_id}/repos/{repo_id}")
    async def investigate(
        repo_id: UUID, access: Annotated[OrgAccess, Depends(org_access)]
    ) -> dict[str, str]:
        authorize(
            access,
            Grant.INVESTIGATE,
            org_id=access.org_id,
            repo_id=str(repo_id),
            code_host=READ,
        )
        return {"ok": "yes"}

    @app.get("/test-only/ci-upload")
    async def upload(
        principal: Annotated[CiUploadPrincipal, Depends(ci_upload_principal)],
    ) -> dict[str, str]:
        return {"repo_id": principal.repo_id}

    secrets_seen: list[str] = [CLIENT_SECRET]
    bodies: list[bytes] = []

    async def call(
        method: str, target: str, headers: list[tuple[str, str]] | None = None
    ) -> RawResponse:
        response = await send_raw(app, method, target, headers)
        bodies.append(response[2])
        return response

    async def sign_in_over_http() -> str:
        status, headers, _ = await call("GET", "/auth/github/login")
        assert status == 302
        bound = set_cookie(headers, "__Host-flakepilot_sign_in")
        for attribute in (
            "HttpOnly",
            "Max-Age=600",
            "Path=/",
            "SameSite=lax",
            "Secure",
        ):
            assert attribute in bound
        state = value_of(bound).split(".")[0]
        assert dict(headers)["location"].endswith(f"state={state}")
        cookie = ("cookie", f"__Host-flakepilot_sign_in={value_of(bound)}")
        callback = f"/auth/github/callback?code=c&state={state}"
        status, headers, _ = await call("GET", callback, [cookie])
        assert (status, dict(headers)["location"]) == (303, "/")
        session = set_cookie(headers, "__Host-flakepilot_session")
        for attribute in (
            "HttpOnly",
            "Max-Age=43200",
            "Path=/",
            "SameSite=lax",
            "Secure",
        ):
            assert attribute in session
        assert "Max-Age=0" in set_cookie(headers, "__Host-flakepilot_sign_in")
        # Replays: no browser binding, or a different state, both fail.
        assert (await call("GET", callback))[0] == 401
        wrong = f"/auth/github/callback?code=c&state={state}x"
        assert (await call("GET", wrong, [cookie]))[0] == 401
        secrets_seen.append(value_of(session))
        return value_of(session)

    def code(response: RawResponse) -> tuple[int, str | None]:
        status, _, body = response
        return status, json.loads(body)["error"]["code"] if status >= 400 else None

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            session = await sign_in_over_http()
            jar = [("cookie", f"__Host-flakepilot_session={session}")]
            repo = f"/test-only/orgs/{ORG_A}/repos"
            assert code(await call("GET", f"{repo}/{REPO_1}")) == (
                401,
                "unauthenticated",
            )
            other = f"/test-only/orgs/{ORG_B}/repos/{REPO_1}"
            assert code(await call("GET", other, jar)) == (404, "not_found")
            assert code(await call("GET", f"{repo}/{REPO_2}", jar)) == (
                403,
                "forbidden",
            )
            assert code(await call("GET", f"{repo}/{REPO_1}", jar)) == (200, None)
            bad_org = f"/test-only/orgs/not-a-uuid/repos/{REPO_1}"
            assert code(await call("GET", bad_org, jar))[0] == 422

            for csrf in (
                [],
                [("origin", "https://evil.example")],
                [("origin", "null")],
            ):
                response = await call("POST", "/auth/logout", jar + csrf)
                assert code(response) == (403, "forbidden")
            response = await call("POST", "/auth/logout", jar + [("origin", ORIGIN)])
            assert response[0] == 204
            assert "Max-Age=0" in set_cookie(response[1], "__Host-flakepilot_session")
            assert code(await call("GET", f"{repo}/{REPO_1}", jar))[0] == 401

            jar = [("cookie", f"__Host-flakepilot_session={await sign_in_over_http()}")]
            referer = [("referer", f"{ORIGIN}/settings")]
            assert (await call("POST", "/auth/logout", jar + referer))[0] == 204

            iam_service: Iam = app.state.iam
            access = await iam_service.org_access(SessionPrincipal(USER_A), ORG_A)
            token = await iam_service.create_ci_upload_token(
                access, org_id=ORG_A, repo_id=REPO_1, code_host=READ
            )
            secrets_seen.append(token)
            bearer = [("authorization", f"Bearer {token}")]
            status, _, body = await call("GET", "/test-only/ci-upload", bearer)
            assert (status, json.loads(body)) == (200, {"repo_id": REPO_1})
            for headers in ([], [("authorization", "Bearer fpi_nope")], jar):
                assert (
                    code(await call("GET", "/test-only/ci-upload", headers))[0] == 401
                )
            principal = await iam_service.verify_ci_upload_token(token)
            assert principal is not None
            await iam_service.revoke_ci_upload_token(
                access,
                org_id=ORG_A,
                repo_id=REPO_1,
                token_id=principal.token_id,
                code_host=READ,
            )
            assert code(await call("GET", "/test-only/ci-upload", bearer))[0] == 401

    asyncio.run(scenario())
    output = capsys.readouterr().err + b"".join(bodies).decode()
    assert not any(secret in output for secret in secrets_seen)
