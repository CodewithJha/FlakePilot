"""IAM's PostgreSQL store. Runs inside a caller's transaction and never commits.

Times come from PostgreSQL (`now()`), so expiry and idle checks agree across processes.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.identity.application.iam import CiUploadPrincipal
from flakepilot.identity.domain.access import Grant, OrgAccess
from flakepilot.identity.infrastructure.tables import (
    ci_upload_tokens,
    memberships,
    orgs,
    repo_grants,
    sessions,
    users,
)


class SqlIamStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_user(self, *, issuer: str, subject: str, login: str) -> str:
        new_user = insert(users).values(
            id=str(uuid.uuid4()),
            idp_issuer=issuer,
            idp_subject=subject,
            github_login=login,
            created_at=func.now(),
        )
        statement = new_user.on_conflict_do_update(
            index_elements=[users.c.idp_issuer, users.c.idp_subject],
            set_={"github_login": new_user.excluded.github_login},
        ).returning(users.c.id)
        user_id: str = (await self._session.execute(statement)).scalar_one()
        return user_id

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

    async def revoke_session(self, *, id_hash: bytes, reason: str) -> None:
        await self._session.execute(
            update(sessions)
            .where(sessions.c.id_hash == id_hash, sessions.c.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoke_reason=reason)
        )

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

    async def org_name_exists(self, *, name: str) -> bool:
        found = await self._session.execute(
            select(orgs.c.id).where(orgs.c.name == name)
        )
        return found.first() is not None

    async def create_org(self, *, org_id: str, name: str) -> None:
        await self._session.execute(
            orgs.insert().values(id=org_id, name=name, created_at=func.now())
        )

    async def ensure_user(self, *, issuer: str, subject: str, login: str | None) -> str:
        """Find or create; a new user without a login gets the subject until sign-in."""
        new_user = insert(users).values(
            id=str(uuid.uuid4()),
            idp_issuer=issuer,
            idp_subject=subject,
            github_login=login or subject,
            created_at=func.now(),
        )
        keys = [users.c.idp_issuer, users.c.idp_subject]
        statement = (
            new_user.on_conflict_do_update(
                index_elements=keys,
                set_={"github_login": new_user.excluded.github_login},
            )
            if login
            else new_user.on_conflict_do_nothing(index_elements=keys)
        )
        await self._session.execute(statement)
        found = await self._session.execute(
            select(users.c.id).where(
                users.c.idp_issuer == issuer, users.c.idp_subject == subject
            )
        )
        user_id: str = found.scalar_one()
        return user_id

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
