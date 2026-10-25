"""`verify-run` / `verify-fetch` against a migrated PostgreSQL.

End-to-end: the wrapper records exit code, hashes and sanitized output in
`ops.verification_runs`; `verify-fetch` reads it back (read-only) and its
verdict is computed only from what the record says. The migration runs as part
of `alembic upgrade head`, so the fixture's migrated database has the table.
"""

import asyncio
import json
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import (
    create_engine,
    transaction,
)
from flakepilot.interface.composition_root.verification_evidence import (
    run_verify_fetch,
    run_verify_run,
)

pytestmark = pytest.mark.integration


async def _rows(engine: AsyncEngine, job_id: str) -> list[dict[str, object]]:
    async with transaction(engine) as session:
        result = await session.execute(
            text(
                "SELECT job_id, exit_code, target_sha256, stdout_sha256, "
                "stderr_sha256, stdout_bytes, timed_out, stdout_head "
                "FROM ops.verification_runs WHERE job_id = :job_id"
            ),
            {"job_id": job_id},
        )
        return [dict(row.mappings()) for row in result]


def test_verify_run_records_and_verify_fetch_checks(
    migrated_database_url: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(Secret(migrated_database_url))
    job_id = f"job-local-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("RENDER_JOB_ID", job_id)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)

    # The controlled outcome Render would have mislabeled: exit 26.
    code = run_verify_run(
        settings,
        ["--", sys.executable, "-c", "import sys; print('CAL26_DONE'); sys.exit(26)"],
    )
    out, _ = capsys.readouterr()
    assert code == 26
    evidence = [ln for ln in out.splitlines() if ln.startswith("VERIFY_EVIDENCE ")][-1]
    payload = json.loads(evidence[len("VERIFY_EVIDENCE ") :])
    assert payload["db_write"] == "ok"
    assert payload["job_id"] == job_id

    engine = create_engine(migrated_database_url)
    try:
        rows = asyncio.run(_rows(engine, job_id))
    finally:
        asyncio.run(engine.dispose())

    assert len(rows) == 1
    row = rows[0]
    assert row["exit_code"] == 26
    assert row["timed_out"] is False
    assert row["target_sha256"] == payload["target_sha256"]
    assert row["stdout_sha256"] == payload["stdout_sha256"]
    assert row["stdout_bytes"] == len(b"CAL26_DONE\n")
    assert "CAL26_DONE" in str(row["stdout_head"])

    # verify-fetch: read-only verdict from the record, matched expectation.
    code = run_verify_fetch(settings, ["--job", job_id, "--expect-exit", "26"])
    out, _ = capsys.readouterr()
    verdict = json.loads(out)
    assert verdict["verdict"] == "pass" and verdict["runs"][0]["exit_code"] == 26
    assert code == 0

    # Wrong expectation fails the verdict, exit 1; unknown job exits 2.
    code = run_verify_fetch(settings, ["--job", job_id, "--expect-exit", "0"])
    out, _ = capsys.readouterr()
    assert json.loads(out)["verdict"] == "fail"
    assert code == 1
    assert run_verify_fetch(settings, ["--job", "job-absent"]) == 2
    capsys.readouterr()
