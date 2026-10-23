"""IAM's PostgreSQL store. Runs inside a caller's transaction and never commits.

Times come from PostgreSQL (`now()`), so expiry and idle checks agree across processes.
"""

import uuid
from datetime import timedelta

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.identity.application.iam import CiUploadPrincipal
from flakepilot.identity.domain.access import Grant, OrgAccess
from flakepilot.identity.infrastructure.tables import (
    ci_upload_tokens,
    memberships,
    orgs,
    outbox,
    repo_grants,
    sessions,
    users,
)
from flakepilot.shared_kernel.events import Event

_IDENTITY = [users.c.idp_issuer, users.c.idp_subject]


class SqlIamStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_event(self, event: Event) -> None:
        await self._session.execute(
            outbox.insert().values(
                event_id=event.event_id,
                name=event.name,
                schema_version=event.schema_version,
                occurred_at=func.now(),
                org_id=event.org_id,
                repo_id=event.repo_id,
                actor_type=event.actor.type.value,
                actor_id=event.actor.id,
                channel=event.origin.channel.value,
                correlation_id=event.origin.correlation_id,
                subject_type=event.subject.type,
                subject_id=event.subject.id,
                before=event.before,
                after=event.after,
                reason=event.reason,
                payload=dict(event.payload),
            )
        )

    async def _create_user(
        self, *, issuer: str, subject: str, login: str
    ) -> str | None:
        """The new user's ID, or None if (issuer, subject) already exists."""
        statement = (
            insert(users)
            .values(
                id=str(uuid.uuid4()),
                idp_issuer=issuer,
                idp_subject=subject,
                github_login=login,
                created_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=_IDENTITY)
            .returning(users.c.id)
        )
        user_id: str | None = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        return user_id

    def _identity(self, issuer: str, subject: str) -> ColumnElement[bool]:
        return (users.c.idp_issuer == issuer) & (users.c.idp_subject == subject)

    async def upsert_user(
        self, *, issuer: str, subject: str, login: str
    ) -> tuple[str, bool]:
        created = await self._create_user(issuer=issuer, subject=subject, login=login)
        if created is not None:
            return created, True
        refreshed = await self._session.execute(
            update(users)
            .where(self._identity(issuer, subject))
            .values(github_login=login)
            .returning(users.c.id)
        )
        user_id: str = refreshed.scalar_one()
        return user_id, False

    async def create_session(
        self, *, id_hash: bytes, user_id: str, lifetime: timedelta
    ) -> None:
        await self._session.execute(
            sessions.insert().values(
                id_hash=id_hash,
                user_id=user_id,
                created_at=func.now(),
                last_seen_at=func.now(),
                expires_at=func.now() + lifetime,
            )
        )

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> str | None:
        revoked = await self._session.execute(
            update(sessions)
            .where(sessions.c.id_hash == id_hash, sessions.c.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoke_reason=reason)
            .returning(sessions.c.user_id)
        )
        user_id: str | None = revoked.scalar_one_or_none()
        return user_id

    async def active_session_user(
        self, *, id_hash: bytes, idle: timedelta, touch_after: timedelta
    ) -> str | None:
        active = (
            (sessions.c.id_hash == id_hash)
            & sessions.c.revoked_at.is_(None)
            & (sessions.c.expires_at > func.now())
            & (sessions.c.last_seen_at > func.now() - idle)
        )
        found = await self._session.execute(select(sessions.c.user_id).where(active))
        user_id: str | None = found.scalar_one_or_none()
        if user_id is not None:
            # Conditional, so concurrent requests write at most once per interval.
            await self._session.execute(
                update(sessions)
                .where(active, sessions.c.last_seen_at < func.now() - touch_after)
                .values(last_seen_at=func.now())
            )
        return user_id

    async def org_exists(self, *, org_id: str) -> bool:
        found = await self._session.execute(
            select(orgs.c.id).where(orgs.c.id == org_id)
        )
        return found.first() is not None

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None:
        membership = await self._session.execute(
            select(memberships.c.org_admin).where(
                memberships.c.org_id == org_id, memberships.c.user_id == user_id
            )
        )
        org_admin = membership.scalar_one_or_none()
        if org_admin is None:
            return None
        rows = await self._session.execute(
            select(repo_grants.c.repo_id, repo_grants.c.grant).where(
                repo_grants.c.org_id == org_id, repo_grants.c.user_id == user_id
            )
        )
        grants = frozenset((repo_id, Grant(grant)) for repo_id, grant in rows)
        return OrgAccess(user_id, org_id, org_admin, grants)

    async def create_ci_upload_token(
        self,
        *,
        token_id: str,
        org_id: str,
        repo_id: str,
        token_hash: bytes,
        display_prefix: str,
        created_by: str,
    ) -> None:
        await self._session.execute(
            ci_upload_tokens.insert().values(
                id=token_id,
                org_id=org_id,
                repo_id=repo_id,
                token_hash=token_hash,
                display_prefix=display_prefix,
                created_by=created_by,
                created_at=func.now(),
            )
        )

    async def revoke_ci_upload_token(
        self, *, org_id: str, repo_id: str, token_id: str
    ) -> bool:
        revoked = await self._session.execute(
            update(ci_upload_tokens)
            .where(
                ci_upload_tokens.c.id == token_id,
                ci_upload_tokens.c.org_id == org_id,
                ci_upload_tokens.c.repo_id == repo_id,
                ci_upload_tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
            .returning(ci_upload_tokens.c.id)
        )
        return revoked.first() is not None

    async def create_org(self, *, org_id: str, name: str) -> bool:
        # Arbitrates on uq_orgs_lower_name: a concurrent insert of an equal name waits
        # for the other transaction, then inserts nothing (no error, nothing partial).
        created = await self._session.execute(
            insert(orgs)
            .values(id=org_id, name=name, created_at=func.now())
            .on_conflict_do_nothing(index_elements=[func.lower(orgs.c.name)])
            .returning(orgs.c.id)
        )
        return created.first() is not None

    async def ensure_user(
        self, *, issuer: str, subject: str, login: str | None
    ) -> tuple[str, bool]:
        """Find or create; a new user without a login gets the subject until sign-in."""
        created = await self._create_user(
            issuer=issuer, subject=subject, login=login or subject
        )
        if created is not None:
            return created, True
        found = (
            update(users)
            .where(self._identity(issuer, subject))
            .values(github_login=login)
            .returning(users.c.id)
            if login
            else select(users.c.id).where(self._identity(issuer, subject))
        )
        user_id: str = (await self._session.execute(found)).scalar_one()
        return user_id, False

    async def add_membership(
        self, *, org_id: str, user_id: str, org_admin: bool
    ) -> None:
        await self._session.execute(
            memberships.insert().values(
                org_id=org_id,
                user_id=user_id,
                org_admin=org_admin,
                created_at=func.now(),
            )
        )

    async def find_user(self, *, issuer: str, subject: str) -> str | None:
        found = await self._session.execute(
            select(users.c.id).where(self._identity(issuer, subject))
        )
        user_id: str | None = found.scalar_one_or_none()
        return user_id

    async def github_login(self, *, user_id: str) -> str | None:
        try:
            uuid.UUID(user_id)
        except ValueError:
            return None
        found = await self._session.execute(
            select(users.c.github_login).where(users.c.id == user_id)
        )
        return found.scalar_one_or_none()

    async def add_grant(
        self,
        *,
        org_id: str,
        user_id: str,
        repo_id: str,
        grant: Grant,
        granted_by: str,
    ) -> bool:
        # Arbitrates only on the primary key: a repeat returns False and writes nothing.
        created = await self._session.execute(
            insert(repo_grants)
            .values(
                org_id=org_id,
                user_id=user_id,
                repo_id=repo_id,
                grant=grant.value,
                granted_by=granted_by,
                created_at=func.now(),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    repo_grants.c.org_id,
                    repo_grants.c.user_id,
                    repo_grants.c.repo_id,
                    repo_grants.c.grant,
                ]
            )
            .returning(repo_grants.c.user_id)
        )
        return created.first() is not None

    async def revoke_grant(
        self, *, org_id: str, user_id: str, repo_id: str, grant: Grant
    ) -> bool:
        gone = await self._session.execute(
            delete(repo_grants)
            .where(
                repo_grants.c.org_id == org_id,
                repo_grants.c.user_id == user_id,
                repo_grants.c.repo_id == repo_id,
                repo_grants.c.grant == grant.value,
            )
            .returning(repo_grants.c.user_id)
        )
        return gone.first() is not None

    async def revoke_user_sessions(self, *, user_id: str, reason: str) -> int:
        revoked = await self._session.execute(
            update(sessions)
            .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoke_reason=reason)
            .returning(sessions.c.user_id)
        )
        return len(revoked.all())

    async def active_ci_upload_token(
        self, *, token_hash: bytes
    ) -> CiUploadPrincipal | None:
        found = await self._session.execute(
            select(
                ci_upload_tokens.c.id,
                ci_upload_tokens.c.org_id,
                ci_upload_tokens.c.repo_id,
            ).where(
                ci_upload_tokens.c.token_hash == token_hash,
                ci_upload_tokens.c.revoked_at.is_(None),
            )
        )
        row = found.first()
        return CiUploadPrincipal(row.id, row.org_id, row.repo_id) if row else None
