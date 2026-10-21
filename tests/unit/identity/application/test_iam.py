import asyncio
import base64
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import (
    AuthNotConfigured,
    AuthProviderUnavailable,
    CiUploadPrincipal,
    Iam,
    IamStore,
    InvalidBootstrapInput,
    OrgAlreadyExists,
    SignInFailed,
    digest,
)
from flakepilot.integrations.application.ports import (
    IdentityProviderError,
    VerifiedIdentity,
)
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Event, Origin

TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
HTTP = Origin(Channel.HTTP, "request-1")
CLI = Origin(Channel.CLI, "run-1")


class FakeProvider:
    def __init__(self, error: str | None = None, unavailable: bool = False) -> None:
        self.error = error
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://idp.test/authorize?state={state}&challenge={code_challenge}"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        self.calls.append((code, code_verifier))
        if self.error:
            raise IdentityProviderError(self.error, unavailable=self.unavailable)
        return VerifiedIdentity("https://github.com", "42", "octo")


class FakeStore:
    def __init__(self, access: OrgAccess | None = None, live_sessions: int = 0) -> None:
        self.access = access
        self.live_sessions = live_sessions
        self.org_names: set[str] = set()
        self.writes: list[tuple[str, dict[str, object]]] = []

    @property
    def events(self) -> list[Event]:
        return [row["event"] for name, row in self.writes if name == "event"]  # type: ignore[misc]

    async def append_event(self, event: Event) -> None:
        self.writes.append(("event", {"event": event}))

    async def upsert_user(
        self, *, issuer: str, subject: str, login: str
    ) -> tuple[str, bool]:
        self.writes.append(("upsert_user", {"subject": subject, "login": login}))
        return "user-1", True

    async def create_session(
        self, *, id_hash: bytes, user_id: str, lifetime: timedelta
    ) -> None:
        self.writes.append(("create_session", {"id_hash": id_hash, "user_id": user_id}))

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> str | None:
        self.writes.append(("revoke_session", {"id_hash": id_hash, "reason": reason}))
        return "user-0"

    async def active_session_user(
        self, *, id_hash: bytes, idle: timedelta, touch_after: timedelta
    ) -> str | None:
        self.writes.append(("active_session_user", {"id_hash": id_hash}))
        return None

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None:
        return self.access

    async def create_ci_upload_token(self, **row: object) -> None:
        self.writes.append(("create_ci_upload_token", row))

    async def revoke_ci_upload_token(
        self, *, org_id: str, repo_id: str, token_id: str
    ) -> bool:
        return True

    async def active_ci_upload_token(
        self, *, token_hash: bytes
    ) -> CiUploadPrincipal | None:
        self.writes.append(("active_ci_upload_token", {"token_hash": token_hash}))
        return None

    async def create_org(self, *, org_id: str, name: str) -> bool:
        if name.lower() in self.org_names:
            return False
        self.org_names.add(name.lower())
        self.writes.append(("create_org", {"org_id": org_id, "name": name}))
        return True

    async def ensure_user(
        self, *, issuer: str, subject: str, login: str | None
    ) -> tuple[str, bool]:
        self.writes.append(("ensure_user", {"subject": subject, "login": login}))
        return "user-1", False

    async def add_membership(
        self, *, org_id: str, user_id: str, org_admin: bool
    ) -> None:
        self.writes.append(("add_membership", {"user_id": user_id, "admin": org_admin}))

    async def revoke_user_sessions(self, *, user_id: str, reason: str) -> int:
        self.writes.append(
            ("revoke_user_sessions", {"user_id": user_id, "reason": reason})
        )
        return self.live_sessions


def iam(store: FakeStore, provider: FakeProvider | None = None) -> Iam:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[IamStore]:
        yield store

    return Iam(unit_of_work, provider)


def complete(service: Iam, **overrides: object) -> str:
    arguments: dict[str, object] = {
        "expected_state": "state-1",
        "received_state": "state-1",
        "code": "code-1",
        "code_verifier": "verifier-1",
        "error": None,
        "previous_session": None,
        "origin": HTTP,
    }
    arguments.update(overrides)
    return asyncio.run(service.complete_sign_in(**arguments))  # type: ignore[arg-type]


def test_sign_in_is_closed_when_not_configured() -> None:
    service = iam(FakeStore(), provider=None)
    with pytest.raises(AuthNotConfigured):
        service.begin_sign_in()
    with pytest.raises(AuthNotConfigured):
        complete(service)


def test_begin_sign_in_uses_fresh_state_and_an_s256_challenge() -> None:
    service = iam(FakeStore(), FakeProvider())
    first, second = service.begin_sign_in(), service.begin_sign_in()
    assert first.state != second.state and TOKEN_FORMAT.fullmatch(first.state)
    sha = hashlib.sha256(first.code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(sha).rstrip(b"=").decode()
    assert first.url.endswith(f"challenge={challenge}") and len(challenge) == 43


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_state": ""},
        {"received_state": None},
        {"received_state": "state-2"},
        {"received_state": "stäte-1"},
        {"code": None},
        {"code_verifier": ""},
        {"error": "access_denied"},
    ],
)
def test_callbacks_that_do_not_match_fail_before_contacting_github(
    overrides: dict[str, object],
) -> None:
    provider = FakeProvider()
    store = FakeStore()
    with pytest.raises(SignInFailed):
        complete(iam(store, provider), **overrides)
    assert provider.calls == [] and store.writes == []


def test_provider_rejection_fails_sign_in() -> None:
    store = FakeStore()
    with pytest.raises(SignInFailed):
        complete(iam(store, FakeProvider(error="code_rejected")))
    assert store.writes == []


def test_provider_outage_is_reported_as_unavailable() -> None:
    store = FakeStore()
    with pytest.raises(AuthProviderUnavailable):
        complete(iam(store, FakeProvider(error="http_502", unavailable=True)))
    assert store.writes == []


def bootstrap(store: FakeStore, **overrides: object) -> str:
    arguments: dict[str, object] = {
        "name": "Acme Inc.",
        "admin_issuer": "https://github.com",
        "admin_subject": "12345",
        "admin_login": "octo-cat",
        "origin": CLI,
    }
    arguments.update(overrides)
    return asyncio.run(iam(store).bootstrap_org(**arguments))  # type: ignore[arg-type]


def test_bootstrap_creates_org_admin_in_one_unit_of_work() -> None:
    store = FakeStore(live_sessions=2)
    org_id = bootstrap(store)
    assert [name for name, _ in store.writes] == [
        "create_org",
        "ensure_user",
        "add_membership",
        "event",
        "revoke_user_sessions",
        "event",
    ]
    assert store.writes[0][1] == {"org_id": org_id, "name": "Acme Inc."}
    assert store.writes[2][1] == {"user_id": "user-1", "admin": True}
    assert store.writes[4][1] == {"user_id": "user-1", "reason": "privilege_change"}
    bootstrapped, revoked = store.events
    operator = Actor(ActorType.OPERATOR_CLI)
    assert (bootstrapped.name, bootstrapped.actor, bootstrapped.origin) == (
        "iam.org.bootstrapped",
        operator,
        CLI,
    )
    assert (bootstrapped.org_id, bootstrapped.subject.id) == (org_id, org_id)
    assert bootstrapped.payload == {
        "admin_user_id": "user-1",
        "admin_user_created": False,
    }
    assert (revoked.name, revoked.actor, revoked.org_id, revoked.reason) == (
        "iam.session.revoked",
        operator,
        None,
        "privilege_change",
    )
    assert (revoked.subject.id, revoked.payload) == ("user-1", {"sessions": 2})


def test_bootstrap_without_live_sessions_emits_no_revocation() -> None:
    store = FakeStore()
    bootstrap(store)
    assert [event.name for event in store.events] == ["iam.org.bootstrapped"]


def test_bootstrap_refuses_a_name_equal_ignoring_case() -> None:
    store = FakeStore()
    bootstrap(store, name="Acme Inc.")
    writes = len(store.writes)
    with pytest.raises(OrgAlreadyExists):
        bootstrap(store, name="ACME inc.")
    assert len(store.writes) == writes


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": ""}, "--name"),
        ({"name": " Acme"}, "--name"),
        ({"name": "x" * 101}, "--name"),
        ({"name": "Acme\nSENTINEL"}, "--name"),
        ({"admin_subject": "0"}, "--admin-github-id"),
        ({"admin_subject": "12a"}, "--admin-github-id"),
        ({"admin_subject": "-5"}, "--admin-github-id"),
        ({"admin_login": "bad login!"}, "--admin-login"),
    ],
)
def test_bootstrap_rejects_invalid_input_before_any_write(
    overrides: dict[str, object], field: str
) -> None:
    store = FakeStore()
    with pytest.raises(InvalidBootstrapInput) as excinfo:
        bootstrap(store, **overrides)
    message = str(excinfo.value)
    assert message.startswith(field)
    assert not any(str(v) in message for v in overrides.values() if v)
    assert store.writes == []


def test_sign_in_stores_only_a_hash_and_rotates_the_previous_session() -> None:
    store = FakeStore()
    previous = "p" * 43
    session_id = complete(iam(store, FakeProvider()), previous_session=previous)
    assert TOKEN_FORMAT.fullmatch(session_id)
    assert [name for name, _ in store.writes] == [
        "upsert_user",
        "event",
        "revoke_session",
        "event",
        "create_session",
        "event",
    ]
    assert store.writes[2][1] == {"id_hash": digest(previous), "reason": "rotated"}
    assert store.writes[4][1]["id_hash"] == digest(session_id)
    assert session_id not in repr(store.writes)

    signed_in, rotated, created = store.events
    user = Actor(ActorType.USER, "user-1")
    assert [(e.name, e.actor, e.origin, e.org_id) for e in store.events] == [
        ("iam.user.signed_in", user, HTTP, None),
        ("iam.session.revoked", user, HTTP, None),
        ("iam.session.created", user, HTTP, None),
    ]
    assert signed_in.payload == {"user_created": True}
    # The rotated session belonged to whoever held the cookie: its owner is the subject.
    assert (rotated.subject.id, rotated.reason) == ("user-0", "rotated")
    assert (created.subject.id, created.after) == ("user-1", "active")
    for secret in (
        session_id,
        previous,
        digest(session_id).hex(),
        digest(previous).hex(),
    ):
        assert secret not in repr(store.events)


def test_sign_out_emits_one_revocation_by_the_session_owner() -> None:
    store = FakeStore()
    session_id = "s" * 43
    asyncio.run(iam(store).sign_out(session_id, origin=HTTP))
    [event] = store.events
    assert (event.name, event.actor, event.reason) == (
        "iam.session.revoked",
        Actor(ActorType.USER, "user-0"),
        "signed_out",
    )
    assert session_id not in repr(event) and digest(session_id).hex() not in repr(event)


def test_malformed_credentials_are_rejected_without_a_lookup() -> None:
    store = FakeStore()
    service = iam(store, FakeProvider())
    for value in ("", "short", "x" * 44, "fpi_" + "x" * 42, "../" * 15):
        assert asyncio.run(service.authenticate(value)) is None
        assert asyncio.run(service.verify_ci_upload_token(value)) is None
    assert store.writes == []


CONFIGURER = OrgAccess(
    "user-1", "org-1", grants=frozenset({("repo-1", Grant.CONFIGURE)})
)
VIEWER = OrgAccess("user-2", "org-1", grants=frozenset({("repo-1", Grant.VIEW)}))


def create_token(service: Iam, access: OrgAccess, repo_id: str = "repo-1") -> str:
    return asyncio.run(
        service.create_ci_upload_token(
            access,
            org_id="org-1",
            repo_id=repo_id,
            code_host=CodeHostAccess.READ,
            origin=HTTP,
        )
    )


def test_ci_upload_tokens_are_shown_once_and_stored_as_hashes() -> None:
    store = FakeStore()
    token = create_token(iam(store), CONFIGURER)
    assert token.startswith("fpi_") and TOKEN_FORMAT.fullmatch(token[4:])
    [(name, row), (appended, _)] = store.writes
    assert (name, appended) == ("create_ci_upload_token", "event")
    [event] = store.events
    assert (event.name, event.actor, event.org_id, event.repo_id) == (
        "iam.ci_upload_token.created",
        Actor(ActorType.USER, "user-1"),
        "org-1",
        "repo-1",
    )
    assert (event.subject.type, event.subject.id) == (
        "ci_upload_token",
        row["token_id"],
    )
    for secret in (token, token[4:12], digest(token).hex()):
        assert secret not in repr(event)
    assert row["token_hash"] == digest(token)
    assert row["display_prefix"] == token[4:12]
    assert (row["org_id"], row["repo_id"], row["created_by"]) == (
        "org-1",
        "repo-1",
        "user-1",
    )
    assert token not in repr(row)


def test_ci_upload_tokens_need_repo_configure() -> None:
    store = FakeStore()
    with pytest.raises(PermissionDenied):
        create_token(iam(store), VIEWER)
    with pytest.raises(ResourceNotFound):
        create_token(iam(store), VIEWER, repo_id="repo-2")
    assert store.writes == []


def test_revoking_a_ci_upload_token_emits_its_revocation() -> None:
    store = FakeStore()
    asyncio.run(
        iam(store).revoke_ci_upload_token(
            CONFIGURER,
            org_id="org-1",
            repo_id="repo-1",
            token_id="token-1",
            code_host=CodeHostAccess.READ,
            origin=HTTP,
        )
    )
    [event] = store.events
    assert (event.name, event.subject.id, event.before, event.after) == (
        "iam.ci_upload_token.revoked",
        "token-1",
        "active",
        "revoked",
    )
