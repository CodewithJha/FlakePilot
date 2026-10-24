"""Operator CLI commands for the MVP demo (read/write paths the API cannot offer yet).

Subcommands of `python -m flakepilot.interface.composition_root`:

- `issue-ci-upload-token --org-id <uuid> --repo-id <uuid>`: issue one CI upload
  token without sign-in. This is the operator path the README documents as the
  only way a CI token can be issued today; the API issues them for
  authenticated users once sign-in is configured. The raw token is printed
  once and never stored; only its SHA-256 is.
- `list-runs --org-id <uuid> --repo-id <uuid> [--limit N] [--format json|table]`:
  list a repository's stored runs, newest first, read-only. The run IDs it
  prints are the ones `ci-run-report` and `GET /orgs/{org_id}/runs/{run_id}`
  accept. Listing is a read, so it is not audited; it mutates nothing.
- `ci-run-report --org-id <uuid> --repo-id <uuid> --run-id <uuid> [options]`:
  render a stored run as read-only JSON or a self-contained HTML file. The
  report says exactly what the stored run says: per-test attempt chains plus
  each test's classification from the deterministic, versioned rule set
  `ti-classification/1`, including its target revision, reason and evidence.
  Nothing here runs code, artifacts stay plaintext files (see the
  artifact-store docstring), and revision verification stays unchecked.

Both run one short-lived engine like `bootstrap-org` and are audited through
their use cases. Failures print the cause class and never a database detail
or a secret.
"""

import argparse
import asyncio
import html
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.identity.application.iam import Iam, InvalidTokenIssue
from flakepilot.interface.composition_root import (
    Settings,
    iam_unit_of_work,
    ti_unit_of_work,
)
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)
from flakepilot.interface.composition_root.reporting import (
    classification_cell,
    fail_cascade,
)
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunIndexReport,
    RunViewer,
)


def _uuid_ok(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _print_failure(context: str, exc: Exception) -> None:
    """Class-only, like the other operator commands: never a driver detail."""
    print(f"{context}: {type(exc).__name__}", file=sys.stderr)


def run_issue_ci_upload_token(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="issue-ci-upload-token",
        description="Issue one CI upload token scoped to one (org, repo) pair.",
    )
    parser.add_argument(
        "--org-id", required=True, help="the org ID printed by bootstrap-org (a UUID)"
    )
    parser.add_argument(
        "--repo-id",
        required=True,
        help="the repository ID this token may upload to (a UUID)",
    )
    args = parser.parse_args(argv)
    try:
        token = asyncio.run(_issue_token(settings, args.org_id, args.repo_id))
    except InvalidTokenIssue as exc:
        _print_failure("issue-ci-upload-token", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        _print_failure("issue-ci-upload-token failed", exc)
        return 1
    print(
        "This token is shown once and never stored by FlakePilot; save it now.",
        file=sys.stderr,
    )
    print(token)
    return 0


def run_ci_run_report(settings: Settings, argv: Sequence[str]) -> int:
    args = _parse_report(argv)
    try:
        report = asyncio.run(_run_report(settings, args))
    except ResourceNotFound:
        print("ci-run-report: no such run in this (org, repo) pair", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        _print_failure("ci-run-report failed", exc)
        return 1
    payload: str
    if args.format == "json":
        payload = _json_report(report)
    else:
        payload = _html_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(payload)
    return 0


def run_list_runs(settings: Settings, argv: Sequence[str]) -> int:
    args = _parse_list_runs(argv)
    if not (_uuid_ok(args.org_id) and _uuid_ok(args.repo_id)):
        # Format refusals name the flag, never the value, like the other gates.
        for flag, value in (
            ("--org-id", args.org_id),
            ("--repo-id", args.repo_id),
        ):
            if not _uuid_ok(value):
                print(f"list-runs: {flag} must be a canonical UUID", file=sys.stderr)
        return 1
    try:
        index = asyncio.run(_list_runs(settings, args))
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        _print_failure("list-runs failed", exc)
        return 1
    if args.format == "json":
        print(json.dumps(_index_json(index), indent=2, default=str))
    else:
        for entry in index.entries:
            received = (
                entry.received_at.isoformat(timespec="seconds")
                if entry.received_at is not None
                else "-"
            )
            print(
                f"{entry.run_id}  {received}  {entry.commit_sha}  "
                f"{entry.environment}  revision={entry.revision_status}  "
                f"passed={entry.summary.passed}/{entry.summary.tests} "
                f"failed={entry.summary.failed}"
            )
        if not index.entries:
            print("no stored runs for this (org, repo) pair yet")
    return 0


def _parse_list_runs(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="list-runs",
        description=(
            "List one repository's stored runs, newest first, read-only "
            "(the run IDs ci-run-report and the session run view accept)."
        ),
    )
    parser.add_argument("--org-id", required=True, help="org ID (a UUID)")
    parser.add_argument("--repo-id", required=True, help="repo ID (a UUID)")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="at most this many rows, newest first (1-200; clamped by the use case)",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser.parse_args(argv)


async def _list_runs(settings: Settings, args: argparse.Namespace) -> RunIndexReport:
    engine = create_engine(settings.database_url.reveal())
    try:
        return await RunViewer(ti_unit_of_work(engine)).runs_index(
            org_id=args.org_id, repo_id=args.repo_id, limit=args.limit
        )
    finally:
        await dispose_engine(engine)


def _index_json(index: RunIndexReport) -> dict[str, Any]:
    return {
        "org_id": index.org_id,
        "repo_id": index.repo_id,
        "runs": [
            {
                "run_id": entry.run_id,
                "source": entry.source,
                "commit_sha": entry.commit_sha,
                "environment": entry.environment,
                "revision_status": entry.revision_status,
                "received_at": (
                    entry.received_at.isoformat() if entry.received_at else None
                ),
                "summary": {
                    "tests": entry.summary.tests,
                    "attempts": entry.summary.attempts,
                    "passed": entry.summary.passed,
                    "failed": entry.summary.failed,
                    "errored": entry.summary.errored,
                    "skipped": entry.summary.skipped,
                },
            }
            for entry in index.entries
        ],
    }


def _parse_report(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ci-run-report",
        description="Render one stored CI run, read-only, for the operator demo.",
    )
    parser.add_argument("--org-id", required=True, help="org ID (a UUID)")
    parser.add_argument("--repo-id", required=True, help="repo ID (a UUID)")
    parser.add_argument("--run-id", required=True, help="run ID (a UUID)")
    parser.add_argument("--format", choices=("html", "json"), default="html")
    parser.add_argument(
        "--out",
        help="write to this path instead of standard output (e.g. run.html)",
    )
    return parser.parse_args(argv)


async def _issue_token(settings: Settings, org_id: str, repo_id: str) -> str:
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        return await iam.issue_ci_upload_token(
            org_id=org_id,
            repo_id=repo_id,
            origin=Origin(Channel.CLI, str(uuid.uuid4())),
        )
    finally:
        await dispose_engine(engine)


async def _run_report(
    settings: Settings, args: argparse.Namespace
) -> ClassifiedRunReport:
    engine = create_engine(settings.database_url.reveal())
    try:
        return await RunViewer(ti_unit_of_work(engine)).classified_report(
            org_id=args.org_id, repo_id=args.repo_id, run_id=args.run_id
        )
    finally:
        await dispose_engine(engine)


def _classification_json(
    classification: Any,
) -> dict[str, Any] | None:
    """Exactly the domain verdict: rule version, class, reason, target, evidence."""
    if classification is None:
        return None
    return {
        "failure_class": classification.failure_class.value,
        "reason": classification.reason.value if classification.reason else None,
        "target_sha": classification.target_sha,
        "rule_version": classification.rule_version,
        "evidence": dict(classification.evidence),
    }


def _summary_json(report: ClassifiedRunReport) -> dict[str, Any]:
    return {
        "run_id": report.run.run_id,
        "org_id": report.run.org_id,
        "repo_id": report.run.repo_id,
        "upload_id": report.run.upload_id,
        "source": report.run.source,
        "commit_sha": report.run.commit_sha,
        "environment": report.run.environment,
        "revision_status": report.run.revision_status,
        "summary": {
            "tests": report.run.summary.tests,
            "attempts": report.run.summary.attempts,
            "passed": report.run.summary.passed,
            "failed": report.run.summary.failed,
            "errored": report.run.summary.errored,
            "skipped": report.run.summary.skipped,
        },
        "results": [
            {
                "classname": entry.case.classname,
                "name": entry.case.name,
                "attempts": list(entry.case.attempts),
                "test_id": entry.case.test_id,
                "classification": _classification_json(entry.classification),
            }
            for entry in report.cases
        ],
    }


def _json_report(report: ClassifiedRunReport) -> str:
    return json.dumps(_summary_json(report), indent=2, default=str) + "\n"


def _html_report(report: ClassifiedRunReport) -> str:
    run = report.run
    metadata = run.metadata
    rows: list[tuple[str, Any]] = [
        ("CI source", metadata.ci_source if metadata else None),
        ("CI run ID", metadata.ci_run_id if metadata else None),
        ("CI attempt", metadata.ci_attempt if metadata else None),
        ("Job name", metadata.job_name if metadata else None),
        ("Commit SHA", run.commit_sha),
        ("Ref", metadata.ref if metadata else None),
        (
            "Environment",
            metadata.environment if metadata else run.environment,
        ),
        ("Started at", metadata.started_at if metadata else None),
        ("Finished at", metadata.finished_at if metadata else None),
        ("Received", run.received_at),
        ("Revision status", run.revision_status),
        ("Raw report size (bytes)", run.size_bytes),
        ("Artifact key (plaintext file)", run.artifact_key),
    ]
    metadata_rows = "\n".join(
        f"<tr><th>{html.escape(str(name))}</th><td>{fail_cascade(value)}</td></tr>"
        for name, value in rows
    )
    case_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(entry.case.classname)}</td>"
        f"<td>{html.escape(entry.case.name)}</td>"
        "<td>"
        f"{' &rarr; '.join(html.escape(attempt) for attempt in entry.case.attempts)}"
        "</td>"
        f"<td>{classification_cell(entry)}</td>"
        "</tr>"
        for entry in report.cases
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FlakePilot run report</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; color: #1a1a2e; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d5d5dc; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f4f4f8; font-weight: 600; }}
  td code {{ font-size: 0.8rem; word-break: break-all; }}
  .summary td:last-child {{ font-weight: 600; }}
  .note {{ color: #555; font-size: 0.8rem; }}
  .muted {{ color: #777; }}
</style>
</head>
<body>
<h1>FlakePilot run report (operator demo)</h1>
<p class="muted">
Run <code>{run.run_id}</code> &middot; org <code>{html.escape(run.org_id)}</code>
&middot; repo <code>{html.escape(run.repo_id)}</code>
&middot; generated {datetime.now(UTC).isoformat(timespec="seconds")}
</p>
<p>This is a read-only view of what one CI upload stored. It does not execute
anything and it is not evidence of code-host verification: the commit SHA is
what CI reported, and the revision status stays <code>{run.revision_status}</code>
in this MVP.</p>

<h2>Summary</h2>
<table class="summary">
<tr><th>Tests</th><td>{run.summary.tests}</td></tr>
<tr><th>Attempts</th><td>{run.summary.attempts}</td></tr>
<tr><th>Passed (final)</th><td>{run.summary.passed}</td></tr>
<tr><th>Failed (final)</th><td>{run.summary.failed}</td></tr>
<tr><th>Errored (final)</th><td>{run.summary.errored}</td></tr>
<tr><th>Skipped</th><td>{run.summary.skipped}</td></tr>
</table>

<h2>Upload</h2>
<table>{metadata_rows}</table>
<p class="note">The raw report body is stored unparsed at the artifact key above
(a plaintext file; encryption is not wired in this MVP).</p>

<h2>Tests, attempts, and classification</h2>
<table>
<tr><th>Classname</th><th>Test name</th><th>Attempts (chronological)</th><th>Classification</th></tr>
{case_rows}
</table>
<p class="note">An attempt chain like &ldquo;failed &rarr; passed&rdquo; is a
recurring failure within the CI run itself: the test failed at least once
before its final outcome. The classification column is the deterministic,
versioned rule set's verdict over this test's stored CI evidence
(<code>ti-classification/1</code>; no confidence scores, no model output).
&ldquo;inconclusive&rdquo; is a real verdict when the stored evidence cannot
support a stronger one &mdash; for example <code>no_passing_baseline</code>
when no earlier CI run passed the test in an environment that fails at the
target revision. Only CI-report evidence counts; FlakePilot's own runs are
excluded and listed as <code>excluded_runs</code>.</p>
</body>
</html>
"""
