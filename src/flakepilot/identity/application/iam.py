"""IAM use cases: sign-in, sessions, org access, CI upload tokens (ADR-011).

Credentials are random, returned raw only to the caller that created them, and stored only
as SHA-256 hashes. Each use case is one unit of work (one transaction); stores never commit.

Not audited yet (AUD is deferred). These state changes need durable audit when it lands:
sign-in (user created or login refreshed, session created), session rotation, sign-out,
CI upload token creation and revocation; later also grant and membership changes,
privilege-change session revocation, and secret writes.
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

    async def upsert_user(self, *, issuer: str, subject: str, login: str) -> str: ...

    async def create_session(
        self, *, id_hash: bytes, user_id: str, lifetime: timedelta
    ) -> None: ...

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> None: ...

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

    async def org_name_exists(self, *, name: str) -> bool: ...

    async def create_org(self, *, org_id: str, name: str) -> None: ...

    async def ensure_user(
        self, *, issuer: str, subject: str, login: str | None
    ) -> str: ...

    async def add_membership(
        self, *, org_id: str, user_id: str, org_admin: bool
    ) -> None: ...


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
            user_id = await store.upsert_user(
                issuer=identity.issuer, subject=identity.subject, login=identity.login
            )
            if previous_session and _SESSION_ID.fullmatch(previous_session):
                await store.revoke_session(
                    id_hash=digest(previous_session), reason="rotated"
                )
            await store.create_session(
                id_hash=digest(session_id), user_id=user_id, lifetime=SESSION_LIFETIME
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

    async def sign_out(self, session_id: str) -> None:
        if _SESSION_ID.fullmatch(session_id):
            async with self._unit_of_work() as store:
                await store.revoke_session(
                    id_hash=digest(session_id), reason="signed_out"
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
    ) -> str:
        """Needs `repo.configure`. Returns the raw token: shown once, never stored."""
        authorize(
            access, Grant.CONFIGURE, org_id=org_id, repo_id=repo_id, code_host=code_host
        )
        token = CI_UPLOAD_TOKEN_PREFIX + secrets.token_urlsafe(32)
        async with self._unit_of_work() as store:
            await store.create_ci_upload_token(
                token_id=str(uuid.uuid4()),
                org_id=org_id,
                repo_id=repo_id,
                token_hash=digest(token),
                display_prefix=token[len(CI_UPLOAD_TOKEN_PREFIX) :][:8],
                created_by=access.principal_id,
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
    ) -> None:
        authorize(
            access, Grant.CONFIGURE, org_id=org_id, repo_id=repo_id, code_host=code_host
        )
        async with self._unit_of_work() as store:
            revoked = await store.revoke_ci_upload_token(
                org_id=org_id, repo_id=repo_id, token_id=token_id
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
    ) -> str:
        """Operator bootstrap: a new org with one org admin, in one transaction.

        The admin user is found or created by (issuer, subject); an existing user keeps
        their login unless one is given. Refuses if any org already has this name.
        Not audited yet (AUD is deferred). Returns the new org's ID.
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
        async with self._unit_of_work() as store:
            if await store.org_name_exists(name=name):
                raise OrgAlreadyExists("an org with this name already exists")
            await store.create_org(org_id=org_id, name=name)
            user_id = await store.ensure_user(
                issuer=admin_issuer, subject=admin_subject, login=admin_login
            )
            await store.add_membership(org_id=org_id, user_id=user_id, org_admin=True)
        return org_id

    async def verify_ci_upload_token(self, token: str) -> CiUploadPrincipal | None:
        if not _CI_UPLOAD_TOKEN.fullmatch(token):
            return None
        async with self._unit_of_work() as store:
            return await store.active_ci_upload_token(token_hash=digest(token))


def _fail(reason: str) -> NoReturn:
    _logger.warning("sign-in failed: %s", reason)
    raise SignInFailed(reason)
