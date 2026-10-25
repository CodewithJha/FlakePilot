"""The cost controls over real PostgreSQL (2026-10-25 hardening).

The AI daily cap (per-org provider-bound assist requests per UTC day) and the
ingest rate limit (per (org, repo, token) processed uploads over a trailing
window). The cap refuses before the in-flight intent row and any provider
contact; the rate limit refuses with a recorded, audited `rate_limited`
rejection. The model here is always the deterministic fake — these are
PostgreSQL tests, not provider verification.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.diagnosis.application.assist import (
    Assist,
    AssistRejected,
    UntrustedPart,
)
from flakepilot.diagnosis.application.store import AiPolicy, DiagStore
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.diagnosis.infrastructure.store import SqlDiagStore
from flakepilot.identity.application.authorization import CodeHostAccess, OrgAccess
from flakepilot.identity.application.iam import CiUploadPrincipal
from flakepilot.integrations.infrastructure.ai import FakeAiAssist
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.ingestion import (
    Ingestion,
    UploadRejected,
)
from flakepilot.test_intelligence.application.store import TiStore
from flakepilot.test_intelligence.domain.results import HEADERS
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration


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


def _diag(
    engine: AsyncEngine,
) -> Callable[[], AbstractAsyncContextManager[DiagStore]]:
    @asynccontextmanager
    async def unit() -> AsyncIterator[DiagStore]:
        async with transaction(engine) as session:
            yield SqlDiagStore(session)

    return unit


def _ti(engine: AsyncEngine) -> Callable[[], AbstractAsyncContextManager[TiStore]]:
    @asynccontextmanager
    async def unit() -> AsyncIterator[TiStore]:
        async with transaction(engine) as session:
            yield SqlTiStore(session)

    return unit


def _report(*cases: tuple[str, str, str]) -> bytes:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{inner}</testcase>'
        for c, n, inner in cases
    )
    return (
        f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}'
        "</testsuite></testsuites>".encode()
    )


def _metadata(n: int) -> dict[str, list[str]]:
    """Distinct idempotency keys, so each upload is a new processed row."""
    return {
        HEADERS["ci_source"]: ["github_actions"],
        HEADERS["ci_run_id"]: [f"cap-{n}"],
        HEADERS["ci_attempt"]: ["1"],
        HEADERS["job_name"]: ["job"],
        HEADERS["commit_sha"]: ["1" * 40],
        HEADERS["ref"]: ["refs/heads/main"],
        HEADERS["environment"]: ["ci"],
        HEADERS["started_at"]: ["2026-10-21T10:00:00+00:00"],
        HEADERS["finished_at"]: ["2026-10-21T10:05:00+00:00"],
    }


def test_the_org_daily_ai_cap_refuses_before_any_provider_or_intent_write(
    migrated_database_url: str,
) -> None:
    org_id, repo_id, actor = (str(uuid.uuid4()) for _ in range(3))
    access = OrgAccess(actor, org_id, org_admin=True)
    origin = Origin(Channel.HTTP, "ai-cap")

    async def scenario(engine: AsyncEngine) -> tuple[str, int, int]:
        service = Assist(_diag(engine), FakeAiAssist(), daily_request_cap=2)
        await service.set_policy(
            access,
            org_id=org_id,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=origin,
        )
        for _ in range(2):
            await service.assist(
                access,
                org_id=org_id,
                repo_id=repo_id,
                code_host=CodeHostAccess.READ,
                instructions="summarize",
                parts=(UntrustedPart("log", "fail"),),
                evidence_ids=frozenset(),
                origin=origin,
            )
        with pytest.raises(AssistRejected) as refused:
            await service.assist(
                access,
                org_id=org_id,
                repo_id=repo_id,
                code_host=CodeHostAccess.READ,
                instructions="summarize",
                parts=(UntrustedPart("log", "fail"),),
                evidence_ids=frozenset(),
                origin=origin,
            )
        rows = await sql(
            engine,
            "SELECT count(*) FROM diag.ai_requests WHERE org_id = :o",
            o=org_id,
        )
        produced = await sql(
            engine,
            "SELECT count(*) FROM diag.ai_requests WHERE org_id = :o "
            "AND outcome = 'produced'",
            o=org_id,
        )
        return refused.value.reason, int(rows[0][0]), int(produced[0][0])

    reason, total, produced = run(migrated_database_url, scenario)
    assert reason == "ai_daily_cap"
    # Exactly the two allowed requests exist, both produced; the denied one
    # wrote no row at all — no in-flight intent, no provider contact.
    assert (total, produced) == (2, 2)


def test_the_ingest_rate_limit_records_audited_rejections_and_then_refuses(
    migrated_database_url: str, tmp_path: Path
) -> None:
    org_id, repo_id = str(uuid.uuid4()), str(uuid.uuid4())
    token_id = str(uuid.uuid4())
    principal = CiUploadPrincipal(token_id, org_id, repo_id)
    origin = Origin(Channel.HTTP, "ingest-cap")

    async def scenario(engine: AsyncEngine) -> tuple[str, str, int, int]:
        service = Ingestion(
            _ti(engine),
            LocalFsArtifactStore(tmp_path),
            JUnitParser(),
            rate_limit=2,
            rate_window_seconds=3600,
        )
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'Cap', now())",
            o=org_id,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:t, :o, :r, :h, '12345678', :t, now(), NULL)",
            t=token_id,
            o=org_id,
            r=repo_id,
            h=b"\x02" * 32,
        )
        # Two processed uploads through the real path (real artifact writes to
        # the temporary root): the window now holds exactly 2 for this token.
        for n in range(2):
            await service.ingest(
                principal,
                headers=_metadata(n),
                content_type="application/xml",
                content_encoding=None,
                body=_report(("pkg.Suite", f"case-{n}", "")),
                origin=origin,
            )
        with pytest.raises(UploadRejected) as first:
            await service.ingest(
                principal,
                headers=_metadata(99),
                content_type="application/xml",
                content_encoding=None,
                body=_report(("pkg.Suite", "over", "")),
                origin=origin,
            )
        with pytest.raises(UploadRejected) as second:
            await service.ingest(
                principal,
                headers=_metadata(100),
                content_type="application/xml",
                content_encoding=None,
                body=_report(("pkg.Suite", "over-2", "")),
                origin=origin,
            )
        rejections = await sql(
            engine,
            "SELECT reject_reason FROM ti.uploads WHERE state = 'rejected' "
            "ORDER BY received_at",
        )
        events = await sql(
            engine,
            "SELECT count(*) FROM ti.outbox WHERE name = 'ti.upload.rejected'",
        )
        return (
            first.value.reason,
            second.value.reason,
            len(rejections),
            int(events[0][0]),
        )

    first_reason, second_reason, rejections, events = run(
        migrated_database_url, scenario
    )
    assert (first_reason, second_reason) == ("rate_limited", "rate_limited")
    # Both refusals are recorded, audited rejections — not silent 429s.
    assert (rejections, events) == (2, 2)
