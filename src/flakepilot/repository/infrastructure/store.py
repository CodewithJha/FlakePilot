"""Repository directory. Runs inside the caller's transaction and never commits."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.repository.application.source import RepoBinding
from flakepilot.repository.infrastructure.tables import repositories


class SqlRepoDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, binding: RepoBinding, *, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("invalid_request")
        await self._session.execute(
            repositories.insert().values(
                id=binding.repo_id,
                org_id=binding.org_id,
                host="github",
                owner=binding.owner,
                name=binding.name,
                created_at=now,
            )
        )

    async def get(self, repo_id: str) -> RepoBinding | None:
        try:
            uuid.UUID(repo_id)
        except ValueError:
            return None
        found = await self._session.execute(
            select(repositories).where(repositories.c.id == repo_id)
        )
        row = found.mappings().first()
        if row is None:
            return None
        return RepoBinding(
            str(row["org_id"]), str(row["id"]), str(row["owner"]), str(row["name"])
        )
