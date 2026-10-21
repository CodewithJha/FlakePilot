"""IAM use cases: sign-in, sessions, org access, CI upload tokens (ADR-011).

Credentials are random, returned raw only to the caller that created them, and stored only
as SHA-256 hashes. Each use case is one unit of work (one transaction); stores never commit.

Invariant for every use case that changes a user's memberships or grants: revoke that
user's sessions (`privilege_change`) in the same unit of work (ADR-011 B, IAM-DATA-MODEL).

Audit (ADR-012): every state change appends one event to IAM's outbox in the same unit of
work, after the change it describes, so a failed append rolls the change back. Events
never carry session IDs, tokens or their hashes. Session-last-seen touches are not events.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, Protocol

from flakepilot.identity.application.authorization import (
    ResourceNotFound,
    authorize,
)
from flakepilot.identity.domain.access import CodeHostAccess, Grant, OrgAccess
from flakepilot.integrations.application.ports import (
    IdentityProvider,
    IdentityProviderError,
)
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

__all__ = [  # IdentityProvider is re-exported: it is the port this API is built with
    "AuthNotConfigured",
    "AuthProviderUnavailable",
    "CiUploadPrincipal",
    "Iam",
    "IamStore",
    "IdentityProvider",
    "InvalidBootstrapInput",
    "OrgAlreadyExists",
    "SessionPrincipal",
    "SignInFailed",
    "SignInStart",
    "UnitOfWork",
    "digest",
]

# ADR-011 B, provisional.
SESSION_LIFETIME = timedelta(hours=12)
SESSION_IDLE = timedelta(hours=2)
SESSION_TOUCH_AFTER = timedelta(minutes=5)

CI_UPLOAD_TOKEN_PREFIX = "fpi_"
_TOKEN_BODY = r"[A-Za-z0-9_-]{43}"  # 32 random bytes, URL-safe base64 without padding
_CI_UPLOAD_TOKEN = re.compile(CI_UPLOAD_TOKEN_PREFIX + _TOKEN_BODY)
_SESSION_ID = re.compile(_TOKEN_BODY)
_ORG_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_PROVIDER_SUBJECT = re.compile(r"[1-9][0-9]{0,19}")  # GitHub user IDs are numeric
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

_logger = logging.getLogger("flakepilot.iam")


def digest(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


class AuthNotConfigured(Exception):
    """Sign-in is not configured; nothing can authenticate (fail closed)."""


class SignInFailed(Exception):
    """The sign-in callback could not be verified (state, code, or identity rejected)."""


class AuthProviderUnavailable(Exception):
    """The identity provider could not answer (network, timeout, 5xx); retry later."""


class InvalidBootstrapInput(ValueError):
    """Bootstrap input rejected. Messages name the field and never include its value."""


class OrgAlreadyExists(Exception):
    """An org with this name already exists; bootstrap does not create a second one."""


@dataclass(frozen=True)
class SessionPrincipal:
    user_id: str


@dataclass(frozen=True)
class CiUploadPrincipal:
    """A CI upload token's principal: may only upload reports to this one repo."""

    token_id: str
    org_id: str
    repo_id: str


@dataclass(frozen=True)
class SignInStart:
    url: str
    state: str
    code_verifier: str


class IamStore(Protocol):
    """IAM persistence inside one unit of work. Implementations never commit."""

    async def append_event(self, event: Event) -> None: ...

    async def upsert_user(
        self, *, issuer: str, subject: str, login: str
    ) -> tuple[str, bool]:
        """(user ID, whether the user was created)."""
        ...

    async def create_session(
        self, *, id_hash: bytes, user_id: str, lifetime: timedelta
    ) -> None: ...

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> str | None:
        """The owning user's ID if an active session was revoked, else None."""
        ...

    async def active_session_user(
        self, *, id_hash: bytes, idle: timedelta, touch_after: timedelta
    ) -> str | None: ...

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None: ...

    async def create_ci_upload_token(
        self,
        *,
        token_id: str,
        org_id: str,
        repo_id: str,
        token_hash: bytes,
        display_prefix: str,
        created_by: str,
    ) -> None: ...

    async def revoke_ci_upload_token(
        self, *, org_id: str, repo_id: str, token_id: str
    ) -> bool: ...

    async def active_ci_upload_token(
        self, *, token_hash: bytes
    ) -> CiUploadPrincipal | None: ...

    async def create_org(self, *, org_id: str, name: str) -> bool:
        """False, writing nothing, if an org name equal ignoring case exists."""
        ...

    async def ensure_user(
        self, *, issuer: str, subject: str, login: str | None
    ) -> tuple[str, bool]:
        """(user ID, whether the user was created)."""
        ...

    async def add_membership(
        self, *, org_id: str, user_id: str, org_admin: bool
    ) -> None: ...

    async def revoke_user_sessions(self, *, user_id: str, reason: str) -> int:
        """The number of active sessions revoked."""
        ...


UnitOfWork = Callable[[], AbstractAsyncContextManager[IamStore]]


class Iam:
    def __init__(
        self, unit_of_work: UnitOfWork, identity_provider: IdentityProvider | None
    ) -> None:
        self._unit_of_work = unit_of_work
        self._provider = identity_provider

    def begin_sign_in(self) -> SignInStart:
        """A fresh `state` and PKCE verifier; the caller binds both to the browser."""
        if self._provider is None:
            raise AuthNotConfigured
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        url = self._provider.authorization_url(
            state=state, code_challenge=challenge.rstrip(b"=").decode()
        )
        return SignInStart(url=url, state=state, code_verifier=verifier)

    async def complete_sign_in(
        self,
        *,
        expected_state: str,
        received_state: str | None,
        code: str | None,
        code_verifier: str,
        error: str | None,
        previous_session: str | None,
        origin: Origin,
    ) -> str:
        """Verify the callback, then create or refresh the user and a new session.

        A previous session in the same browser is revoked (`rotated`). Returns the new
        session ID, which only the browser cookie ever holds.
        """
        if self._provider is None:
            raise AuthNotConfigured
        if error is not None:
            _fail("provider_error")
        if not (expected_state and received_state and code and code_verifier):
            _fail("missing_state_or_code")
        if not hmac.compare_digest(expected_state.encode(), received_state.encode()):
            _fail("state_mismatch")
        try:
            identity = await self._provider.identify(
                code=code, code_verifier=code_verifier
            )
        except IdentityProviderError as exc:
            if exc.unavailable:
                _logger.warning("sign-in provider unavailable: %s", exc.reason)
                raise AuthProviderUnavailable(exc.reason) from None
            _fail(exc.reason)
        session_id = secrets.token_urlsafe(32)
        async with self._unit_of_work() as store:
            user_id, created = await store.upsert_user(
                issuer=identity.issuer, subject=identity.subject, login=identity.login
            )
            actor = Actor(ActorType.USER, user_id)
            await store.append_event(
                Event(
                    "iam.user.signed_in",
                    actor,
                    origin,
                    Subject("user", user_id),
                    payload={"user_created": created},
                )
            )
            if previous_session and _SESSION_ID.fullmatch(previous_session):
                owner = await store.revoke_session(
                    id_hash=digest(previous_session), reason="rotated"
                )
                if owner is not None:
                    await store.append_event(
                        _sessions_revoked(actor, origin, owner, "rotated", 1)
                    )
            await store.create_session(
                id_hash=digest(session_id), user_id=user_id, lifetime=SESSION_LIFETIME
            )
            await store.append_event(
                Event(
                    "iam.session.created",
                    actor,
                    origin,
                    Subject("user", user_id),
                    after="active",
                )
            )
        return session_id

    async def authenticate(self, session_id: str) -> SessionPrincipal | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        async with self._unit_of_work() as store:
            user_id = await store.active_session_user(
                id_hash=digest(session_id),
                idle=SESSION_IDLE,
                touch_after=SESSION_TOUCH_AFTER,
            )
        return SessionPrincipal(user_id) if user_id else None

    async def sign_out(self, session_id: str, *, origin: Origin) -> None:
        if _SESSION_ID.fullmatch(session_id):
            async with self._unit_of_work() as store:
                owner = await store.revoke_session(
                    id_hash=digest(session_id), reason="signed_out"
                )
                if owner is not None:
                    actor = Actor(ActorType.USER, owner)
                    await store.append_event(
                        _sessions_revoked(actor, origin, owner, "signed_out", 1)
                    )

    async def org_access(self, principal: SessionPrincipal, org_id: str) -> OrgAccess:
        """The principal's standing in the org, from IAM's records. Not a member, or no
        such org: ResourceNotFound (the org ID is only a lookup key, never proof)."""
        async with self._unit_of_work() as store:
            access = await store.org_access(user_id=principal.user_id, org_id=org_id)
        if access is None:
            raise ResourceNotFound
        return access

    async def create_ci_upload_token(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        code_host: CodeHostAccess,
        origin: Origin,
    ) -> str:
        """Needs `repo.configure`. Returns the raw token: shown once, never stored."""
        authorize(
            access, Grant.CONFIGURE, org_id=org_id, repo_id=repo_id, code_host=code_host
        )
        token = CI_UPLOAD_TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = str(uuid.uuid4())
        async with self._unit_of_work() as store:
            await store.create_ci_upload_token(
                token_id=token_id,
                org_id=org_id,
                repo_id=repo_id,
                token_hash=digest(token),
                display_prefix=token[len(CI_UPLOAD_TOKEN_PREFIX) :][:8],
                created_by=access.principal_id,
            )
            await store.append_event(
                Event(
                    "iam.ci_upload_token.created",
                    Actor(ActorType.USER, access.principal_id),
                    origin,
                    Subject("ci_upload_token", token_id),
                    org_id=org_id,
                    repo_id=repo_id,
                    after="active",
                )
            )
        return token

    async def revoke_ci_upload_token(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        token_id: str,
        code_host: CodeHostAccess,
        origin: Origin,
    ) -> None:
        authorize(
            access, Grant.CONFIGURE, org_id=org_id, repo_id=repo_id, code_host=code_host
        )
        async with self._unit_of_work() as store:
            revoked = await store.revoke_ci_upload_token(
                org_id=org_id, repo_id=repo_id, token_id=token_id
            )
            if revoked:
                await store.append_event(
                    Event(
                        "iam.ci_upload_token.revoked",
                        Actor(ActorType.USER, access.principal_id),
                        origin,
                        Subject("ci_upload_token", token_id),
                        org_id=org_id,
                        repo_id=repo_id,
                        before="active",
                        after="revoked",
                    )
                )
        if not revoked:
            raise ResourceNotFound

    async def bootstrap_org(
        self,
        *,
        name: str,
        admin_issuer: str,
        admin_subject: str,
        admin_login: str | None,
        origin: Origin,
    ) -> str:
        """Operator bootstrap: a new org with one org admin, in one transaction.

        The admin user is found or created by (issuer, subject); an existing user keeps
        their login unless one is given. Becoming org admin is a privilege change, so the
        user's active sessions are revoked (`privilege_change`) in the same transaction:
        sessions are per user, not per org. Refuses, writing nothing, if an org name
        equal ignoring case exists (enforced by a unique index, so concurrent runs are
        safe). Returns the new org's ID.
        """
        if name != name.strip() or not _ORG_NAME.fullmatch(name):
            raise InvalidBootstrapInput(
                "--name must be 1-100 printable characters without surrounding spaces"
            )
        if not _PROVIDER_SUBJECT.fullmatch(admin_subject):
            raise InvalidBootstrapInput("--admin-github-id must be a positive number")
        if admin_login is not None and not _LOGIN.fullmatch(admin_login):
            raise InvalidBootstrapInput("--admin-login is not a valid GitHub login")
        org_id = str(uuid.uuid4())
        operator = Actor(ActorType.OPERATOR_CLI)
        async with self._unit_of_work() as store:
            if not await store.create_org(org_id=org_id, name=name):
                raise OrgAlreadyExists("an org with this name already exists")
            user_id, created = await store.ensure_user(
                issuer=admin_issuer, subject=admin_subject, login=admin_login
            )
            await store.add_membership(org_id=org_id, user_id=user_id, org_admin=True)
            await store.append_event(
                Event(
                    "iam.org.bootstrapped",
                    operator,
                    origin,
                    Subject("org", org_id),
                    org_id=org_id,
                    payload={"admin_user_id": user_id, "admin_user_created": created},
                )
            )
            revoked = await store.revoke_user_sessions(
                user_id=user_id, reason="privilege_change"
            )
            if revoked:
                await store.append_event(
                    _sessions_revoked(
                        operator, origin, user_id, "privilege_change", revoked
                    )
                )
        return org_id

    async def verify_ci_upload_token(self, token: str) -> CiUploadPrincipal | None:
        if not _CI_UPLOAD_TOKEN.fullmatch(token):
            return None
        async with self._unit_of_work() as store:
            return await store.active_ci_upload_token(token_hash=digest(token))


def _sessions_revoked(
    actor: Actor, origin: Origin, user_id: str, reason: str, count: int
) -> Event:
    """Sessions are identified only by their owner: no session ID or hash in events."""
    return Event(
        "iam.session.revoked",
        actor,
        origin,
        Subject("user", user_id),
        before="active",
        after="revoked",
        reason=reason,
        payload={"sessions": count},
    )


def _fail(reason: str) -> NoReturn:
    _logger.warning("sign-in failed: %s", reason)
    raise SignInFailed(reason)
