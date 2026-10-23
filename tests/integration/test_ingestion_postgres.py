"""JUnit ingestion and classification against real PostgreSQL, over HTTP where it matters.
Artifacts go to a pytest temporary directory, never into the repository."""

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import exc, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message
from support import InMemoryArtifactStore, RawResponse, send_raw

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    OrgAccess,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import CiUploadPrincipal, digest
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import Channel, Event, Origin
from flakepilot.test_intelligence.application.classification import TestClassification
from flakepilot.test_intelligence.application.ingestion import Ingestion
from flakepilot.test_intelligence.application.run_view import RunViewer
from flakepilot.test_intelligence.application.store import TiStore
from flakepilot.test_intelligence.domain.classification import FailureClass, Reason
from flakepilot.test_intelligence.domain.results import HEADERS
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

ORG_A, ORG_B = str(uuid.uuid4()), str(uuid.uuid4())
REPO_1, REPO_2, REPO_3 = (str(uuid.uuid4()) for _ in range(3))
TOKEN_A, TOKEN_B, TOKEN_C, REVOKED = (
    "fpi_" + secrets.token_urlsafe(32) for _ in range(4)
)
TOKEN_IDS = {token: str(uuid.uuid4()) for token in (TOKEN_A, TOKEN_B, TOKEN_C, REVOKED)}
SCOPES = {
    TOKEN_A: (ORG_A, REPO_1),
    TOKEN_B: (ORG_B, REPO_2),
    TOKEN_C: (ORG_A, REPO_3),
    REVOKED: (ORG_A, REPO_1),
}
OLD, NEW = "1" * 40, "2" * 40
SENTINEL = "SENTINEL-ingest-55"
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


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    async def seed(engine: AsyncEngine) -> None:
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) "
            "VALUES (:a, 'Ingest A', now()), (:b, 'Ingest B', now())",
            a=ORG_A,
            b=ORG_B,
        )
        for token, (org, repo) in SCOPES.items():
            await sql(
                engine,
                "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
                "display_prefix, created_by, created_at, revoked_at) VALUES "
                "(:id, :org, :repo, :h, :p, :id, now(), "
                f"{'now()' if token == REVOKED else 'NULL'})",
                id=TOKEN_IDS[token],
                org=org,
                repo=repo,
                h=digest(token),
                p=token[4:12],
            )

    run(migrated_database_url, seed)
    return migrated_database_url


@pytest.fixture(scope="module")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("artifacts")


def report(*cases: tuple[str, str, str]) -> bytes:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{inner}</testcase>'
        for c, n, inner in cases
    )
    return f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>'.encode()


FAIL = "<failure>assertion</failure>"


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


def upload_headers(
    token: str, meta: list[tuple[str, str]], content_type: str = "application/xml"
) -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {token}"), ("content-type", content_type), *meta]


class Client:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.bodies: list[bytes] = []

    async def upload(
        self,
        token: str,
        body: bytes,
        meta: list[tuple[str, str]],
        extra: list[tuple[str, str]] | None = None,
        content_type: str = "application/xml",
    ) -> tuple[int, dict[str, Any]]:
        status, _, content = await send_raw(
            self.app,
            "POST",
            "/ingest/junit",
            upload_headers(token, meta, content_type) + (extra or []),
            body,
        )
        self.bodies.append(content)
        return status, json.loads(content)


@asynccontextmanager
async def client(url: str, artifact_dir: Path) -> AsyncIterator[Client]:
    app = create_app(Settings(Secret(url), artifact_dir=artifact_dir))
    async with app.router.lifespan_context(app):
        yield Client(app)


async def events(engine: AsyncEngine, where: str, **params: object) -> list[Any]:
    return await sql(
        engine,
        "SELECT name, org_id::text, repo_id::text, actor_type, actor_id::text, "
        "subject_id::text, before, after, reason, payload::text, correlation_id "
        f"FROM ti.outbox WHERE {where} ORDER BY seq",
        **params,
    )


def test_an_upload_is_processed_stored_and_audited(
    url: str, artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = report(
        ("pkg.Suite", "passes", ""),
        ("pkg.Suite", f"fails {SENTINEL}", FAIL),
        ("pkg.Suite", "retried", FAIL),
        ("pkg.Suite", "retried", ""),  # a repeated case: merged as a second attempt
        ("pkg.Suite", "skips", "<skipped/>"),
    )

    async def scenario(engine: AsyncEngine) -> None:
        async with client(url, artifact_dir) as api:
            status, summary = await api.upload(TOKEN_A, body, metadata())
        assert status == 201, summary
        assert summary["duplicate"] is False and summary["state"] == "processed"
        assert (summary["tests"], summary["attempts"]) == (4, 5)
        assert summary["outcomes"] == {
            "passed": 2,
            "failed": 1,
            "errored": 0,
            "skipped": 1,
        }
        [up] = await sql(
            engine,
            "SELECT org_id::text, repo_id::text, token_id::text, state, artifact_key, "
            "content_sha256, size_bytes, content_type FROM ti.uploads WHERE id = :id",
            id=summary["upload_id"],
        )
        assert (up.org_id, up.repo_id, up.token_id, up.state) == (
            ORG_A,
            REPO_1,
            TOKEN_IDS[TOKEN_A],
            "processed",
        )
        stored = artifact_dir.joinpath(*up.artifact_key.split("/"))
        assert stored.read_bytes() == body and up.size_bytes == len(body)
        assert up.artifact_key.startswith(f"ti/{ORG_A}/{REPO_1}/sha256/")
        [rn] = await sql(
            engine,
            "SELECT source, revision_status, commit_sha FROM ti.runs WHERE id = :id",
            id=summary["run_id"],
        )
        assert tuple(rn) == ("ci_upload", "unchecked", NEW)
        attempts = await sql(
            engine,
            "SELECT i.name, r.attempt, r.outcome FROM ti.test_results r "
            "JOIN ti.test_identities i ON i.id = r.test_id "
            "WHERE r.run_id = :run AND i.name = 'retried' ORDER BY r.attempt",
            run=summary["run_id"],
        )
        assert [tuple(a) for a in attempts] == [
            ("retried", 1, "failed"),
            ("retried", 2, "passed"),
        ]
        audit = await events(engine, "subject_id = :id", id=summary["upload_id"])
        assert [(e.name, e.before, e.after) for e in audit] == [
            ("ti.upload.received", None, "received"),
            ("ti.upload.processed", "received", "processed"),
        ]
        assert {(e.org_id, e.repo_id, e.actor_type, e.actor_id) for e in audit} == {
            (ORG_A, REPO_1, "ci_token", TOKEN_IDS[TOKEN_A])
        }
        assert json.loads(audit[1].payload) == {
            "run_id": summary["run_id"],
            "tests": 4,
            "attempts": 5,
        }
        leaked = repr([tuple(e) for e in audit]) + capsys.readouterr().err
        for secret in (TOKEN_A, SENTINEL, "pkg.Suite", "retried", "<testcase"):
            assert secret not in leaked

    run(url, scenario)


def test_duplicates_return_the_original_and_conflicts_are_rejected(
    url: str, artifact_dir: Path
) -> None:
    body = report(("c", "t", ""))
    key = metadata()

    async def scenario(engine: AsyncEngine) -> None:
        async with client(url, artifact_dir) as api:
            first = await api.upload(TOKEN_A, body, key)
            again = await api.upload(TOKEN_A, body, key)
            conflict = await api.upload(TOKEN_A, report(("c", "t", FAIL)), key)
            after = await api.upload(TOKEN_A, body, key)
        assert first[0] == 201 and again[0] == after[0] == 200
        assert again[1]["duplicate"] is True
        for repeat in (again[1], after[1]):
            assert (repeat["upload_id"], repeat["run_id"]) == (
                first[1]["upload_id"],
                first[1]["run_id"],
            )
            assert repeat["tests"] == 1
        assert conflict[0] == 409
        # The conflicting content was refused before anything was written.
        conflicting = hashlib.sha256(report(("c", "t", FAIL))).hexdigest()
        assert not (
            artifact_dir / "ti" / ORG_A / REPO_1 / "sha256" / conflicting
        ).exists()
        assert conflict[1]["error"]["code"] == "idempotency_conflict"
        ci_run_id = dict(key)[HEADERS["ci_run_id"]]
        rows = await sql(
            engine,
            "SELECT state, reject_reason FROM ti.uploads WHERE ci_run_id = :r "
            "ORDER BY received_at, state",
            r=ci_run_id,
        )
        assert sorted(tuple(r) for r in rows) == [
            ("processed", None),
            ("rejected", "idempotency_conflict"),
        ]
        [[runs]] = await sql(
            engine,
            "SELECT count(*) FROM ti.runs r JOIN ti.uploads u ON u.id = r.upload_id "
            "WHERE u.ci_run_id = :r",
            r=ci_run_id,
        )
        assert runs == 1
        audit = await events(
            engine,
            "name IN ('ti.upload.duplicate', 'ti.upload.rejected') AND "
            "subject_id IN (SELECT id FROM ti.uploads WHERE ci_run_id = :r)",
            r=ci_run_id,
        )
        assert [(e.name, e.reason) for e in audit] == [
            ("ti.upload.duplicate", None),
            ("ti.upload.rejected", "idempotency_conflict"),
            ("ti.upload.duplicate", None),
        ]

    run(url, scenario)


def test_concurrent_submissions_of_one_key_create_exactly_one_run(
    url: str, artifact_dir: Path
) -> None:
    same, other = metadata(), metadata()
    body = report(("c", "concurrent", ""))

    async def scenario(engine: AsyncEngine) -> None:
        async with client(url, artifact_dir) as api:
            identical = await asyncio.gather(
                *(api.upload(TOKEN_A, body, same) for _ in range(8))
            )
            mixed = await asyncio.gather(
                *(
                    api.upload(TOKEN_A, report(("c", f"v{i}", "")), other)
                    for i in range(6)
                )
            )
        assert sorted(s for s, _ in identical) == [200] * 7 + [201]
        assert len({r["run_id"] for _, r in identical}) == 1
        assert sorted(s for s, _ in mixed) == [201] + [409] * 5
        for meta in (same, other):
            [[count]] = await sql(
                engine,
                "SELECT count(*) FROM ti.uploads WHERE state = 'processed' "
                "AND ci_run_id = :r",
                r=dict(meta)[HEADERS["ci_run_id"]],
            )
            assert count == 1

    run(url, scenario)


BILLION_LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
    b'<testsuite><testcase name="&c;"/></testsuite>'
)


def test_rejections_are_recorded_audited_and_never_echo_input(
    url: str, artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = report(("c", "t", ""))
    cases: list[tuple[str, dict[str, Any], int, str, str | None]] = [
        ("text/plain", {}, 415, "unsupported_media_type", None),
        ("application/xml", {"encoding": "gzip"}, 415, "unsupported_media_type", None),
        ("application/xml", {"sha": "Z" + SENTINEL}, 422, "validation_error", None),
        (
            "application/xml",
            {"body": b"<testsuite>" + SENTINEL.encode()},
            422,
            "invalid_report",
            "malformed_xml",
        ),
        (
            "application/xml",
            {"body": BILLION_LAUGHS},
            422,
            "invalid_report",
            "forbidden_dtd",
        ),
        (
            "application/xml",
            {"body": b"<testsuite>" * 40 + b"</testsuite>" * 40},
            422,
            "invalid_report",
            "too_deep",
        ),
        (
            "application/xml",
            {"length": str(11 * 1024 * 1024)},
            413,
            "payload_too_large",
            None,
        ),
    ]

    async def scenario(engine: AsyncEngine) -> None:
        correlation: list[str] = []
        async with client(url, artifact_dir) as api:
            for content_type, change, status, code, reason in cases:
                meta = metadata(
                    **({"commit_sha": change["sha"]} if "sha" in change else {})
                )
                extra = []
                if "encoding" in change:
                    extra.append(("content-encoding", change["encoding"]))
                if "length" in change:
                    extra.append(("content-length", change["length"]))
                started = time.monotonic()
                got, body = await api.upload(
                    TOKEN_A, change.get("body", good), meta, extra, content_type
                )
                assert time.monotonic() - started < 2
                assert (got, body["error"]["code"]) == (status, code), change
                assert body["error"].get("reason") == reason
                if code == "validation_error":
                    assert body["error"]["fields"] == [
                        {"loc": ["header", HEADERS["commit_sha"]], "type": "invalid"}
                    ]
                correlation.append(body["error"]["request_id"])
            leaked = b"".join(api.bodies).decode()
        audit = await events(engine, "correlation_id = ANY(:c)", c=correlation)
        assert [e.reason for e in audit] == [
            "unsupported_media_type",
            "unsupported_media_type",
            "invalid_metadata",
            "malformed_xml",
            "forbidden_dtd",
            "too_deep",
            "too_large",
        ]
        assert {(e.name, e.after, e.actor_id) for e in audit} == {
            ("ti.upload.rejected", "rejected", TOKEN_IDS[TOKEN_A])
        }
        rows = await sql(
            engine,
            "SELECT state, reject_reason, token_id::text, commit_sha FROM ti.uploads "
            "WHERE id = ANY(:ids)",
            ids=[e.subject_id for e in audit],
        )
        assert {(r.state, r.token_id) for r in rows} == {
            ("rejected", TOKEN_IDS[TOKEN_A])
        }
        leaked += repr([tuple(e) for e in audit] + [tuple(r) for r in rows])
        leaked += capsys.readouterr().err
        for secret in (SENTINEL, TOKEN_A, "aaaaaaaaaa"):
            assert secret not in leaked

    run(url, scenario)


ENCODINGS = [
    "UTF-32",
    "UTF-7",
    "Shift_JIS",
    "Big5",
    "GB2312",
    "base64",
    "rot13",
    "x-nope",
]


def test_undecodable_encodings_and_repeated_headers_are_audited_rejections(
    url: str, artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        correlation: list[str] = []
        async with client(url, artifact_dir) as api:
            for encoding in ENCODINGS:
                body = (
                    f'<?xml version="1.0" encoding="{encoding}"?>'
                    '<testsuite><testcase name="t"/></testsuite>'
                ).encode()
                status, error = await api.upload(TOKEN_A, body, metadata())
                assert (status, error["error"]["code"]) == (422, "invalid_report")
                assert error["error"]["reason"] == "malformed_xml"
                correlation.append(error["error"]["request_id"])
            twice = [(HEADERS["job_name"], f"second {SENTINEL}")]
            status, error = await api.upload(
                TOKEN_A, report(("c", "t", "")), metadata(), twice
            )
            assert (status, error["error"]["code"]) == (422, "validation_error")
            assert error["error"]["fields"] == [
                {"loc": ["header", HEADERS["job_name"]], "type": "invalid"}
            ]
            correlation.append(error["error"]["request_id"])
            leaked = b"".join(api.bodies).decode()
        audit = await events(engine, "correlation_id = ANY(:c)", c=correlation)
        assert [(e.name, e.reason) for e in audit] == [
            ("ti.upload.rejected", "malformed_xml")
        ] * len(ENCODINGS) + [("ti.upload.rejected", "invalid_metadata")]
        [[rejected]] = await sql(
            engine,
            "SELECT count(*) FROM ti.uploads WHERE state = 'rejected' AND id = ANY(:ids)",
            ids=[e.subject_id for e in audit],
        )
        assert rejected == len(ENCODINGS) + 1
        logs = capsys.readouterr().err
        assert "Traceback" not in logs and SENTINEL not in leaked + logs

    run(url, scenario)


def test_an_oversized_body_is_refused_while_streaming(
    url: str, artifact_dir: Path
) -> None:
    chunk = b"<" * (1024 * 1024)
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def scenario(engine: AsyncEngine) -> tuple[int, dict[str, Any]]:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        app = create_app(Settings(Secret(url), artifact_dir=artifact_dir))
        headers = upload_headers(TOKEN_A, metadata())
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/ingest/junit",
            "raw_path": b"/ingest/junit",
            "query_string": b"",
            "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in headers],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=10)
        start = next(m for m in sent if m["type"] == "http.response.start")
        content = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return start["status"], json.loads(content)

    status, body = run(url, scenario)
    assert (status, body["error"]["code"]) == (413, "payload_too_large")
    assert reads <= 12  # stopped just past the 10 MiB limit, never read to the end


def test_the_token_alone_decides_the_tenant(url: str, artifact_dir: Path) -> None:
    body = report(("c", "scoped", ""))

    async def scenario(engine: AsyncEngine) -> None:
        async with client(url, artifact_dir) as api:
            spoof = [("x-flakepilot-org-id", ORG_A), ("x-flakepilot-repo-id", REPO_1)]
            status, summary = await api.upload(TOKEN_B, body, metadata(), spoof)
            assert status == 201
            for token in (REVOKED, "fpi_" + secrets.token_urlsafe(32), "nope"):
                got, error = await api.upload(token, body, metadata())
                assert (got, error["error"]["code"]) == (401, "unauthenticated")
        [row] = await sql(
            engine,
            "SELECT org_id::text, repo_id::text FROM ti.runs WHERE id = :id",
            id=summary["run_id"],
        )
        assert tuple(row) == (ORG_B, REPO_2)
        [[unattributed]] = await sql(
            engine,
            "SELECT count(*) FROM ti.uploads WHERE token_id <> ALL(:ids)",
            ids=list(TOKEN_IDS.values()),
        )
        assert unattributed == 0

    run(url, scenario)


async def identity_id(engine: AsyncEngine, repo: str, name: str) -> str:
    [[found]] = await sql(
        engine,
        "SELECT id::text FROM ti.test_identities WHERE repo_id = :r AND name = :n",
        r=repo,
        n=name,
    )
    return str(found)


def test_classification_over_ingested_history(url: str, artifact_dir: Path) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        async with client(url, artifact_dir) as api:
            uploads = [
                (
                    OLD,
                    "1",
                    [
                        ("c", "regress", ""),
                        ("c", "flaky", "<flakyFailure/>"),
                        ("c", "nobase", FAIL),
                    ],
                ),
                (NEW, "2", [("c", "regress", FAIL), ("c", "nobase", FAIL)]),
                (
                    NEW,
                    "1",
                    [("c", "regress", FAIL), ("c", "nobase", FAIL)],
                ),  # attempt 1 after 2
            ]
            for sha, attempt, cases in uploads:
                status, _ = await api.upload(
                    TOKEN_C,
                    report(*cases),
                    metadata(
                        commit_sha=sha, ci_attempt=attempt, ci_run_id=f"h-{sha[0]}"
                    ),
                )
                assert status == 201
        ids = {
            n: await identity_id(engine, REPO_3, n)
            for n in ("regress", "flaky", "nobase")
        }
        # D5: a FlakePilot sandbox run that passed must not count as evidence.
        upload_id, sandbox_run = str(uuid.uuid4()), str(uuid.uuid4())
        await sql(
            engine,
            "INSERT INTO ti.uploads (id, org_id, repo_id, token_id, state, received_at) "
            "VALUES (:u, :o, :r, :t, 'received', now())",
            u=upload_id,
            o=ORG_A,
            r=REPO_3,
            t=TOKEN_IDS[TOKEN_C],
        )
        await sql(
            engine,
            "INSERT INTO ti.runs (id, org_id, repo_id, upload_id, source, commit_sha, "
            "environment, revision_status, received_at, tests, attempts, passed, failed, "
            "errored, skipped) VALUES (:run, :o, :r, :u, 'sandbox', :sha, 'ubuntu-24.04', "
            "'unchecked', now(), 1, 1, 1, 0, 0, 0)",
            run=sandbox_run,
            o=ORG_A,
            r=REPO_3,
            u=upload_id,
            sha=NEW,
        )
        await sql(
            engine,
            "INSERT INTO ti.test_results (org_id, repo_id, run_id, test_id, attempt, "
            "outcome) VALUES (:o, :r, :run, :t, 1, 'passed')",
            o=ORG_A,
            r=REPO_3,
            run=sandbox_run,
            t=ids["nobase"],
        )

        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[TiStore]:
            async with transaction(engine) as session:
                yield SqlTiStore(session)

        query = TestClassification(unit_of_work)
        admin_a = OrgAccess("admin", ORG_A, org_admin=True)

        async def classify(access: OrgAccess, repo: str, name: str) -> Any:
            return await query.classify(
                access,
                org_id=access.org_id,
                repo_id=repo,
                test_id=ids[name],
                code_host=READ,
            )

        regress = await classify(admin_a, REPO_3, "regress")
        assert (regress.failure_class, regress.target_sha) == (
            FailureClass.DETERMINISTIC_REGRESSION,
            NEW,
        )
        assert regress.evidence["baseline_sha"] == OLD
        assert (
            await classify(admin_a, REPO_3, "flaky")
        ).failure_class is FailureClass.FLAKY
        nobase = await classify(admin_a, REPO_3, "nobase")
        assert (nobase.failure_class, nobase.reason) == (
            FailureClass.INCONCLUSIVE,
            Reason.NO_PASSING_BASELINE,
        )
        assert nobase.evidence["excluded_runs"] == [sandbox_run]
        # Other org, other repo, unknown or malformed IDs: not found.
        for access, repo, test in (
            (OrgAccess("admin", ORG_B, org_admin=True), REPO_3, ids["regress"]),
            (admin_a, REPO_1, ids["regress"]),
            (admin_a, REPO_3, str(uuid.uuid4())),
            (admin_a, REPO_3, "not-a-uuid"),
        ):
            with pytest.raises(ResourceNotFound):
                await query.classify(
                    access,
                    org_id=access.org_id,
                    repo_id=repo,
                    test_id=test,
                    code_host=READ,
                )
        with pytest.raises(ResourceNotFound):
            await query.classify(
                admin_a,
                org_id=ORG_B,
                repo_id=REPO_3,
                test_id=ids["regress"],
                code_host=READ,
            )

    run(url, scenario)


def test_run_report_classification_over_ingested_history(
    url: str, artifact_dir: Path
) -> None:
    """The operator report classifies each reported test from stored evidence.

    Hand-written upload bodies throughout: no repository code runs anywhere.
    """

    async def scenario(engine: AsyncEngine) -> None:
        org, repo = str(uuid.uuid4()), str(uuid.uuid4())
        token = "fpi_" + secrets.token_urlsafe(32)
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'RunView', now())",
            o=org,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:id, :org, :repo, :h, :p, :id, now(), NULL)",
            id=str(uuid.uuid4()),
            org=org,
            repo=repo,
            h=digest(token),
            p=token[4:12],
        )
        async with client(url, artifact_dir) as api:
            status, _ = await api.upload(
                token,
                report(
                    ("vr", "view-flaky", ""),
                    ("vr", "view-regress", ""),
                    ("vr", "view-single", ""),
                    ("vr", "view-clean", ""),
                ),
                metadata(commit_sha=OLD, ci_run_id="v-0"),
            )
            assert status == 201
            status, content = await api.upload(
                token,
                report(
                    ("vr", "view-flaky", "<flakyFailure/>"),
                    ("vr", "view-regress", FAIL + "<rerunFailure/>"),
                    ("vr", "view-single", FAIL),
                    ("vr", "view-clean", ""),
                ),
                metadata(commit_sha=NEW, ci_run_id="v-1"),
            )
            assert status == 201
            run_id = content["run_id"]
        names = ("view-flaky", "view-regress", "view-single", "view-clean")
        ids = {n: await identity_id(engine, repo, n) for n in names}

        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[TiStore]:
            async with transaction(engine) as session:
                yield SqlTiStore(session)

        detailed = await RunViewer(unit_of_work).classified_report(
            org_id=org, repo_id=repo, run_id=run_id
        )
        by_name = {entry.case.name: entry for entry in detailed.cases}
        assert set(by_name) == set(names)
        # The identity key is what the evidence query read with.
        for name, entry in by_name.items():
            assert entry.case.test_id == ids[name]
        flaky = by_name["view-flaky"].classification
        assert flaky is not None
        assert (flaky.failure_class, flaky.reason, flaky.target_sha) == (
            FailureClass.FLAKY,
            None,
            NEW,
        )
        regress = by_name["view-regress"].classification
        assert regress is not None
        assert (regress.failure_class, regress.reason) == (
            FailureClass.DETERMINISTIC_REGRESSION,
            None,
        )
        assert regress.target_sha == NEW
        assert regress.evidence["baseline_sha"] == OLD
        single = by_name["view-single"].classification
        assert single is not None
        assert (single.failure_class, single.reason) == (
            FailureClass.INCONCLUSIVE,
            Reason.INSUFFICIENT_HISTORY,
        )
        clean = by_name["view-clean"].classification
        assert clean is not None
        assert (clean.failure_class, clean.reason) == (
            FailureClass.INCONCLUSIVE,
            Reason.INSUFFICIENT_HISTORY,
        )

    run(url, scenario)


class FailingOutbox(SqlTiStore):
    """Appends every event twice: the second insert violates the primary key."""

    async def append_event(self, event: Event) -> None:
        await super().append_event(event)
        await super().append_event(event)


def test_a_failed_outbox_write_rolls_back_the_whole_upload(url: str) -> None:
    principal = CiUploadPrincipal(TOKEN_IDS[TOKEN_A], ORG_A, REPO_1)
    origin = Origin(Channel.HTTP, "rollback-request")
    meta = {name: [value] for name, value in metadata(ci_run_id="rollback")}

    async def scenario(engine: AsyncEngine) -> InMemoryArtifactStore:
        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[TiStore]:
            async with transaction(engine) as session:
                yield FailingOutbox(session)

        artifacts = InMemoryArtifactStore()
        ingestion = Ingestion(unit_of_work, artifacts, JUnitParser())
        for body in (report(("c", "rolled-back", "")), b"<broken"):
            with pytest.raises(exc.IntegrityError):
                await ingestion.ingest(
                    principal,
                    headers=meta,
                    content_type="text/xml; charset=utf-8",
                    content_encoding=None,
                    body=body,
                    origin=origin,
                )
        counts = await sql(
            engine,
            "SELECT (SELECT count(*) FROM ti.uploads WHERE ci_run_id = 'rollback' "
            "OR (state = 'rejected' AND reject_reason = 'malformed_xml' "
            "AND id IN (SELECT subject_id FROM ti.outbox WHERE correlation_id = :c))), "
            "(SELECT count(*) FROM ti.test_identities WHERE name = 'rolled-back'), "
            "(SELECT count(*) FROM ti.outbox WHERE correlation_id = :c)",
            c=origin.correlation_id,
        )
        assert tuple(counts[0]) == (0, 0, 0)
        return artifacts

    artifacts = run(url, scenario)
    # The artifact was written before the transaction: an orphan, content-addressed, so
    # a retry reuses it (ADR-013).
    assert len(artifacts.objects) == 1


def test_ingestion_fails_closed_without_artifact_storage(url: str) -> None:
    app = create_app(Settings(Secret(url)))

    async def scenario(engine: AsyncEngine) -> RawResponse:
        async with app.router.lifespan_context(app):
            return await send_raw(
                app,
                "POST",
                "/ingest/junit",
                upload_headers(TOKEN_A, metadata()),
                report(("c", "t", "")),
            )

    status, _, body = run(url, scenario)
    assert (status, json.loads(body)["error"]["code"]) == (
        503,
        "ingestion_not_configured",
    )


class _UnusedIdentityProvider:
    """Satisfies create_app's factory signature; sign-in endpoints are never hit."""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        raise AssertionError("sign-in is not part of the run-view test")

    async def identify(self, *, code: str, code_verifier: str) -> Any:
        raise AssertionError("sign-in is not part of the run-view test")


VIEW_COOKIE = "__Host-flakepilot_session"


def test_run_view_authorizes_a_signed_in_member_over_real_tables(
    url: str, artifact_dir: Path
) -> None:
    """The HTTP run view: real sessions, memberships, grants, and evidence.

    One authorized member sees the run view with the rule set's verdict; a
    member without `repo.view`, a stranger's session, and no session at all
    learn nothing (404/404/401). All through the real `Iam` and real SQL.
    """

    async def scenario(engine: AsyncEngine) -> None:
        org, repo = str(uuid.uuid4()), str(uuid.uuid4())
        token = "fpi_" + secrets.token_urlsafe(32)
        viewer, no_grant, outsider = (str(uuid.uuid4()) for _ in range(3))
        ok_session, grantless_session, stray_session = (
            secrets.token_urlsafe(32) for _ in range(3)
        )
        await sql(
            engine,
            "INSERT INTO iam.orgs (id, name, created_at) VALUES (:o, 'RunViewHttp', now())",
            o=org,
        )
        await sql(
            engine,
            "INSERT INTO iam.ci_upload_tokens (id, org_id, repo_id, token_hash, "
            "display_prefix, created_by, created_at, revoked_at) VALUES "
            "(:id, :org, :repo, :h, :p, :id, now(), NULL)",
            id=str(uuid.uuid4()),
            org=org,
            repo=repo,
            h=digest(token),
            p=token[4:12],
        )
        for user, issuer_subject in (
            (viewer, "v-1"),
            (no_grant, "n-1"),
            (outsider, "o-1"),
        ):
            await sql(
                engine,
                "INSERT INTO iam.users (id, idp_issuer, idp_subject, github_login, "
                "created_at) VALUES (:u, 'https://github.test', :s, :s, now())",
                u=user,
                s=issuer_subject,
            )
            await sql(
                engine,
                "INSERT INTO iam.memberships (org_id, user_id, org_admin, created_at) "
                "VALUES (:o, :u, FALSE, now())",
                o=org,
                u=user,
            )
        await sql(
            engine,
            'INSERT INTO iam.repo_grants (org_id, user_id, repo_id, "grant", '
            "granted_by, created_at) VALUES (:o, :u, :r, 'repo.view', :u, now())",
            o=org,
            u=viewer,
            r=repo,
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
            status, _ = await api.upload(
                token,
                report(("vr", "view-flaky", ""), ("vr", "view-regress", "")),
                metadata(commit_sha=OLD, ci_run_id="hv-0"),
            )
            assert status == 201
            status, content = await api.upload(
                token,
                report(
                    ("vr", "view-flaky", "<flakyFailure/>"),
                    ("vr", "view-regress", FAIL + "<rerunFailure/>"),
                ),
                metadata(commit_sha=NEW, ci_run_id="hv-1"),
            )
            assert status == 201
            run_id = content["run_id"]

        app = create_app(
            Settings(
                Secret(url),
                artifact_dir=artifact_dir,
                public_origin="https://flakpilot-view.test",
            ),
            identity_provider_factory=lambda _: _UnusedIdentityProvider(),
        )
        async with app.router.lifespan_context(app):

            async def get(path: str, session_id: str | None) -> tuple[int, str]:
                headers = (
                    []
                    if session_id is None
                    else [("cookie", f"{VIEW_COOKIE}={session_id}")]
                )
                got, _, body = await send_raw(app, "GET", path, headers)
                return got, body.decode()

            path = f"/orgs/{org}/runs/{run_id}"
            granted, page = await get(path, ok_session)
            assert granted == 200
            assert "flaky" in page and "deterministic_regression" in page
            assert "unchecked" in page
            grantless, _ = await get(path, grantless_session)
            stranger, _ = await get(path, stray_session)
            anonymous, _ = await get(path, None)
            assert (grantless, stranger, anonymous) == (404, 404, 401)
            # A session value that matches nothing is just unauthenticated.
            assert (await get(path, "not-a-session"))[0] == 401

    run(url, scenario)
