"""Execution evidence for one-off jobs: `verify-run` and `verify-fetch`.

Why this exists: a Render one-off job reports only a coarse `status`
(`succeeded`/`failed`/`canceled`) and no exit code, and calibration run against
the production service on 2026-10-25 showed that status is NOT a function of
the command's exit code:

    python -c 'import sys; sys.exit(1)'    ->  status "succeeded"
    python -c 'import sys; sys.exit(3)'    ->  status "succeeded"
    python -c 'import sys; sys.exit(26)'   ->  status "succeeded"
    python /tmp/does_not_exist.py (exit 2) ->  status "failed"
    sh -c '… last command exits 0'         ->  status "failed"

So `status` must never be treated as evidence that a command succeeded, and the
public API exposes no exit-code field and no machine-readable job logs. The
reliable mechanism is the command reporting for itself:

- `verify-run -- <argv…>` runs the target as a subprocess (via
  `subprocess.run`, no shell), records the real exit code, in-container UTC
  timestamps, the exact target command and SHA-256 hashes of its output, and
  inserts the sanitized record into the durable append-only
  `ops.verification_runs` table (migration `ops_0001`). It then prints a single
  `VERIFY_EVIDENCE {…}` line and exits with the target's own exit code
  (127 if the target could not start, 124 on timeout). A Render `status`
  proves nothing; this record and its printed line are the evidence.
- `verify-fetch --job <id> [--expect-exit N]` reads that record back
  (read-only) and answers whether the recorded outcome matches the
  expectation.

Output is sanitized before storage (secret-shaped assignments, credentialed
URLs, bearer tokens; everything else kept, head and tail, with the raw byte
counts and SHA-256 hashes of the unsanitized streams so truncation is
detectable). The record never carries secret values, and nothing is printed
that the target did not already print to Render's own logs.

This is operator tooling owned by the composition root, not a bounded context:
the `ops` schema is intentionally outside every context's schema and outside
alembic autogenerate's comparison scope.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from flakepilot.interface.composition_root import Settings
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)

PROG_RUN = "verify-run"
PROG_FETCH = "verify-fetch"

MECHANISM_VERSION = "verify-run/1"

#: timeout(1) and Docker conventions: target timed out / could not start.
TIMEOUT_EXIT = 124
SPAWN_EXIT = 127

#: how much of each stream is stored (characters) around the middle cut
STORE_LIMIT = 600

_INSERT_SQL = """
INSERT INTO ops.verification_runs (
    id, job_id, service_id, target_command, target_sha256,
    started_at, finished_at, duration_ms, exit_code, signal, timed_out,
    spawn_error, stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
    stdout_head, stdout_tail, stderr_head, stderr_tail, recorded_at, recorded_by
) VALUES (
    :id, :job_id, :service_id, :target_command, :target_sha256,
    :started_at, :finished_at, :duration_ms, :exit_code, :signal, :timed_out,
    :spawn_error, :stdout_sha256, :stderr_sha256, :stdout_bytes, :stderr_bytes,
    :stdout_head, :stdout_tail, :stderr_head, :stderr_tail, now(), :recorded_by
)
"""

_SELECT_SQL = """
SELECT id, job_id, service_id, target_command, target_sha256,
       started_at, finished_at, duration_ms, exit_code, signal, timed_out,
       spawn_error, stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
       stdout_head, stdout_tail, stderr_head, stderr_tail, recorded_at
FROM ops.verification_runs
{where}
ORDER BY started_at DESC
LIMIT :limit
"""

# --- sanitization -----------------------------------------------------------

_SECRET_NAME_CORE = (
    r"[A-Za-z0-9_]*(?:token|secret|password|passwd|credential|api[_-]?key"
    r"|dsn|auth|bearer)[A-Za-z0-9_]*"
)
_ASSIGNMENT = re.compile(
    rf"({_SECRET_NAME_CORE})(\s*[=:]\s*)(\S+)", flags=re.IGNORECASE
)
_CREDENTIALED_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^@\s/\"]+@")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_RENDER_KEY = re.compile(r"(rnd_[A-Za-z0-9]+)")

_REDACTED = "<redacted>"


def sanitize(text_value: str) -> tuple[str, int]:
    """Redact secret-shaped text; return (sanitized, redaction_count).

    Secret-shaped means: `NAME=value` or `NAME: value` where NAME mentions a
    secret ("token", "key", "secret", "password", "credential", "dsn", "auth",
    "bearer"), a URL with embedded user:password credentials, a bearer token,
    or a Render API key. Everything else is kept verbatim: hashes and printed
    diagnostics are evidence, not secrets.
    """
    count = 0

    def _bump(match: re.Match[str], replacement: str) -> str:
        nonlocal count
        count += 1
        return replacement

    text_value = _BEARER.sub(  # first: so `AUTH: <word> <token>` loses both
        lambda m: m.group(1) + _bump(m, _REDACTED), text_value
    )
    text_value = _ASSIGNMENT.sub(
        lambda m: m.group(1) + m.group(2) + _bump(m, _REDACTED), text_value
    )
    text_value = _CREDENTIALED_URL.sub(
        lambda m: _bump(m, m.group(1) + _REDACTED + "@"), text_value
    )
    text_value = _RENDER_KEY.sub(lambda m: _bump(m, _REDACTED), text_value)
    return text_value, count


def _store_slice(value: str) -> str | None:
    """Head+tail slice with an explicit middle marker, or None when empty."""
    if not value:
        return None
    if len(value) <= 2 * STORE_LIMIT:
        return value
    return value[:STORE_LIMIT] + "\n[…snip…]\n" + value[-STORE_LIMIT:]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the evidence record ----------------------------------------------------


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def run_target(argv: Sequence[str], timeout_seconds: float) -> dict[str, Any]:
    """Run the target and distill it into an evidence dict. No DB, no printing."""
    command_text = shlex.join(argv)
    started = _utcnow()
    spawn_error: str | None = None
    exit_code: int | None = None
    signal_number: int | None = None
    timed_out = False
    stdout_raw = stderr_raw = b""
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        if exit_code < 0:  # killed by signal N: no exit code, record the signal
            signal_number = -exit_code
            exit_code = None
        stdout_raw, stderr_raw = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_raw = exc.stdout or b""
        stderr_raw = exc.stderr or b""
    except FileNotFoundError as exc:
        spawn_error = type(exc).__name__
    except (OSError, ValueError) as exc:
        spawn_error = type(exc).__name__
    finished = _utcnow()
    start_dt = datetime.fromisoformat(started)
    finish_dt = datetime.fromisoformat(finished)
    duration_ms = round((finish_dt - start_dt).total_seconds() * 1000)

    stdout_text = stdout_raw.decode(errors="replace")
    stderr_text = stderr_raw.decode(errors="replace")
    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "mechanism": MECHANISM_VERSION,
        "job_id": os.environ.get("RENDER_JOB_ID"),
        "service_id": os.environ.get("RENDER_SERVICE_ID"),
        "target_command": command_text,
        "target_sha256": _sha256(command_text.encode()),
        "started_at": started,
        "finished_at": finished,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "signal": signal_number,
        "timed_out": timed_out,
        "spawn_error": spawn_error,
        "stdout_sha256": _sha256(stdout_raw) if stdout_raw else "",
        "stderr_sha256": _sha256(stderr_raw) if stderr_raw else "",
        "stdout_bytes": len(stdout_raw),
        "stderr_bytes": len(stderr_raw),
        "stdout_head": None,
        "stdout_tail": None,
        "stderr_head": None,
        "stderr_tail": None,
    }
    if stdout_text:
        sanitized, _ = sanitize(stdout_text)
        record["stdout_head"], record["stdout_tail"] = _split(sanitized)
    if stderr_text:
        sanitized, _ = sanitize(stderr_text)
        record["stderr_head"], record["stderr_tail"] = _split(sanitized)
    return record


def _split(value: str) -> tuple[str, str]:
    if len(value) <= STORE_LIMIT:
        return value, value
    return value[: STORE_LIMIT // 2], value[-STORE_LIMIT // 2 :]


def evidence_line(record: dict[str, Any]) -> str:
    """The single machine-readable line printed to stdout (Render log fallback)."""
    compact = dict(record)
    # keep the line readable and non-secret: sanitized tails, bounded
    compact["job_id"] = record["job_id"] or "local"
    compact["service_id"] = record["service_id"] or "local"
    return "VERIFY_EVIDENCE " + json.dumps(
        compact, sort_keys=True, separators=(",", ":")
    )


async def _persist(settings: Settings, record: dict[str, Any]) -> str | None:
    """Insert the record; return '' when stored, or the exception class name."""
    engine = create_engine(settings.database_url.reveal())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(_INSERT_SQL),
                {
                    **record,
                    "job_id": record["job_id"],
                    "service_id": record["service_id"],
                    "stdout_head": record["stdout_head"],
                    "stdout_tail": record["stdout_tail"],
                    "stderr_head": record["stderr_head"],
                    "stderr_tail": record["stderr_tail"],
                    "spawn_error": record["spawn_error"],
                    "signal": record["signal"],
                    "recorded_by": MECHANISM_VERSION,
                },
            )
        return ""
    except Exception as exc:  # noqa: BLE001 - operator CLI: cause class only
        return type(exc).__name__
    finally:
        await dispose_engine(engine)


def run_verify_run(settings: Settings, argv: Sequence[str]) -> int:
    """`verify-run [--timeout S] [--receipt-url URL] -- <argv…>`."""
    parser = argparse.ArgumentParser(
        prog=PROG_RUN,
        description=(
            "Run a target command as evidenced subprocess: records the real "
            "exit code, timestamps, command identity and sanitized output "
            "hashes into ops.verification_runs and prints one "
            "VERIFY_EVIDENCE line. Exits with the target's own exit code. "
            "A Render one-off job's `status` is NOT evidence; this is."
        ),
        epilog=(
            "One-off jobs do not mount the artifact disk, so run as "
            "`env -u FLAKEPILOT_ARTIFACT_DIR python -m "
            "flakepilot.interface.composition_root verify-run -- <argv…>`"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Kill the target after this many seconds (default 900).",
    )
    parser.add_argument(
        "--receipt-url",
        default=None,
        help=(
            "Optional operator-provided URL to POST the compact sanitized "
            "evidence to (the durable record is the database row; the "
            "receipt is a convenience). Never put secrets there; the "
            "payload is the same sanitized summary printed to stdout."
        ),
    )
    parser.add_argument(
        "target",
        nargs=argparse.REMAINDER,
        help="The target command as argv, after `--`.",
    )
    args = parser.parse_args(argv)
    target = list(args.target)
    if target and target[0] == "--":
        target = target[1:]
    if not target:
        print(f"{PROG_RUN}: no target command after --", file=sys.stderr)
        return 2

    record = run_target(target, args.timeout)

    if args.receipt_url:
        receipt_status = _post_receipt(args.receipt_url, record)
        print(f"VERIFY_RECEIPT {receipt_status}", file=sys.stderr)

    db_error = asyncio.run(_persist(settings, record))
    record["db_write"] = "ok" if db_error == "" else f"failed:{db_error}"

    print(evidence_line(record))
    if record["spawn_error"] is not None:
        return SPAWN_EXIT
    if record["timed_out"]:
        return TIMEOUT_EXIT
    assert record["exit_code"] is not None
    return int(record["exit_code"])


def _post_receipt(url: str, record: dict[str, Any]) -> str:
    """POST the compact evidence summary. Failures never affect the exit code."""
    import urllib.request

    payload = json.dumps(
        {
            "id": record["id"],
            "job_id": record["job_id"] or "local",
            "target_sha256": record["target_sha256"],
            "exit_code": record["exit_code"],
            "signal": record["signal"],
            "timed_out": record["timed_out"],
            "spawn_error": record["spawn_error"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "stdout_sha256": record["stdout_sha256"],
            "stdout_tail": record["stdout_tail"],
            "stderr_tail": record["stderr_tail"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": MECHANISM_VERSION},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return f"{type(response).__name__}:{response.status}"
    except Exception as exc:  # noqa: BLE001 - receipt is best-effort
        return f"failed:{type(exc).__name__}"


# --- read-back --------------------------------------------------------------

_VERDICT_KEYS = ("id", "job_id", "exit_code", "signal", "timed_out", "spawn_error")


async def _fetch_rows(
    settings: Settings, selector: str | None, limit: int
) -> list[dict[str, Any]]:
    where = "WHERE job_id = :selector" if selector else ""
    engine = create_engine(settings.database_url.reveal())
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(_SELECT_SQL.format(where=where)),
                        {"selector": selector, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]
    finally:
        await dispose_engine(engine)


def evaluate_expectations(
    row: dict[str, Any],
    expect_exit: int | None,
    expect_output_contains: str | None,
) -> bool:
    """Verdict against the recorded exit code and sanitized stdout only.

    The Render job `status` is deliberately never consulted: it is not a
    function of the command's exit code (module docstring, 2026-10-25
    calibration).
    """
    if expect_exit is not None and row.get("exit_code") != expect_exit:
        return False
    if expect_output_contains is not None:
        combined = (row.get("stdout_head") or "") + (row.get("stdout_tail") or "")
        if expect_output_contains not in combined:
            return False
    return True


def run_verify_fetch(settings: Settings, argv: Sequence[str]) -> int:
    """`verify-fetch [--job <id>] [--expect-exit N] [--expect-output-contains S]`."""
    parser = argparse.ArgumentParser(
        prog=PROG_FETCH,
        description=(
            "Read recorded verify-run evidence (read-only) and check it "
            "against expectations. Exit 0 when every expectation is met, 1 "
            "when not, 2 when no record matches."
        ),
    )
    parser.add_argument("--job", default=None, help="Filter by job id.")
    parser.add_argument("--expect-exit", type=int, default=None)
    parser.add_argument("--expect-output-contains", default=None)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        rows = asyncio.run(_fetch_rows(settings, args.job, args.limit))
    except Exception as exc:  # noqa: BLE001 - operator CLI: cause class only
        print(f"{PROG_FETCH}: {type(exc).__name__}", file=sys.stderr)
        return 3

    if not rows:
        print(json.dumps({"verdict": "no_record", "matched": 0}))
        return 2

    summaries: list[dict[str, Any]] = []
    verdict = True
    for row in rows:
        ok = evaluate_expectations(row, args.expect_exit, args.expect_output_contains)
        verdict = verdict and ok
        summaries.append(
            {
                **{key: row.get(key) for key in _VERDICT_KEYS if key in row},
                "started_at": row["started_at"].isoformat()
                if row.get("started_at")
                else None,
                "finished_at": row["finished_at"].isoformat()
                if row.get("finished_at")
                else None,
                "expectation_met": ok,
            }
        )

    print(
        json.dumps(
            {
                "verdict": "pass" if verdict else "fail",
                "matched": len(summaries),
                "runs": summaries,
            },
            sort_keys=True,
            default=str,
        )
    )
    return 0 if verdict else 1
