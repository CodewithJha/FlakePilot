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
- `backfill-ci-history --org-id <uuid> --repo-id <uuid> [--max-runs N]
  [--workflow FILE] [--branch NAME] [--artifact-pattern GLOB]`: import the
  JUnit XML that the registered repository's GitHub Actions runs uploaded as
  artifacts, with each run's original completion time (`Backfill`). The
  repository is the pair's registered binding, never a flag; the GitHub App
  token is actions:read on that one repository. Idempotent per (run, attempt,
  artifact); every outcome is a `ti.backfill.*` event. Prints a JSON summary.

All run one short-lived engine like `bootstrap-org` and are audited through
their use cases. Failures print the cause class and never a database detail
or a secret.
"""

import argparse
import asyncio
import fnmatch
import html
import json
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.identity.application.iam import Iam, InvalidTokenIssue
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.github_actions import (
    CodeHostError,
    GitHubActionsHistory,
    ReportRejected,
    RunArtifact,
    WorkflowRun,
    extract_reports,
)
from flakepilot.integrations.infrastructure.installation import InstallationMinter
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root import (
    Settings,
    iam_unit_of_work,
    ti_unit_of_work,
)
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
    transaction,
)
from flakepilot.interface.composition_root.reporting import (
    classification_cell,
    fail_cascade,
)
from flakepilot.interface.http.ui import (
    DESIGN_TOKENS_CSS,
    PAGE_CSS,
)

# The operator report file is viewed offline (its own <style> block, no
# network, no font files). It shares the token set and report CSS with
# `/ui.css`. The served pages use the same palette from the React build.
# Font tokens name Fraunces, Literata and Fragment Mono and fall back to
# Georgia and a system monospace when those faces are not installed.
OFFLINE_REPORT_CSS = (
    DESIGN_TOKENS_CSS
    + PAGE_CSS
    + ".report-body{max-width:60rem;margin:0 auto;padding:2rem 1rem}"
    + ".summary td:last-child{font-weight:600}"
    + "h2{margin-top:1.6rem}table{margin-bottom:1rem}"
)
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.backfill import (
    Backfill,
    BackfillResult,
    BackfillStatus,
    HistoricalReport,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunIndexReport,
    RunViewer,
)

MAX_BACKFILL_RUNS = 50
MAX_ARTIFACTS_PER_RUN = 10
MAX_BACKFILL_ARTIFACTS = 100


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


def _bounded_runs(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_BACKFILL_RUNS:
        raise argparse.ArgumentTypeError(
            f"must be an integer from 1 to {MAX_BACKFILL_RUNS}"
        )
    return int(value)


def _parse_backfill(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill-ci-history",
        description=(
            "Import the registered repository's GitHub Actions JUnit artifacts "
            "with their original CI completion time (operator trust, audited)."
        ),
    )
    parser.add_argument("--org-id", required=True, help="org ID (a UUID)")
    parser.add_argument("--repo-id", required=True, help="registered repo ID (a UUID)")
    parser.add_argument(
        "--max-runs",
        type=_bounded_runs,
        default=20,
        help=f"newest completed runs to read (1-{MAX_BACKFILL_RUNS})",
    )
    parser.add_argument("--workflow", help="only this workflow file or ID")
    parser.add_argument("--branch", help="only runs on this branch")
    parser.add_argument(
        "--artifact-pattern",
        default="*",
        help="artifact names to download (shell glob, case-sensitive)",
    )
    return parser.parse_args(argv)


def run_backfill_ci_history(settings: Settings, argv: Sequence[str]) -> int:
    args = _parse_backfill(argv)
    for flag, value in (("--org-id", args.org_id), ("--repo-id", args.repo_id)):
        if not _uuid_ok(value):
            print(
                f"backfill-ci-history: {flag} must be a canonical UUID", file=sys.stderr
            )
            return 1
    creds = settings.github_installation
    if creds is None or settings.artifact_dir is None:
        print(
            "backfill-ci-history: needs FLAKEPILOT_GITHUB_APP_ID, "
            "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY and FLAKEPILOT_ARTIFACT_DIR",
            file=sys.stderr,
        )
        return 1
    history = GitHubActionsHistory(
        InstallationMinter(
            app_id=creds.app_id,
            private_key_pem=creds.private_key.reveal(),
            scope="actions",
        )
    )

    async def main() -> dict[str, Any]:
        engine = create_engine(settings.database_url.reveal())
        try:
            assert settings.artifact_dir is not None
            return await backfill_ci_history(
                engine,
                history=history,
                backfill=Backfill(
                    ti_unit_of_work(engine),
                    LocalFsArtifactStore(settings.artifact_dir),
                    JUnitParser(),
                ),
                org_id=args.org_id,
                repo_id=args.repo_id,
                max_runs=args.max_runs,
                workflow=args.workflow,
                branch=args.branch,
                pattern=args.artifact_pattern,
            )
        finally:
            await dispose_engine(engine)

    try:
        summary = asyncio.run(main())
    except ResourceNotFound:
        print(
            "backfill-ci-history: no such registered repository in this org",
            file=sys.stderr,
        )
        return 1
    except CodeHostError as exc:
        print(f"backfill-ci-history: GitHub refused: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        _print_failure("backfill-ci-history failed", exc)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


def _environment(artifact: str) -> str:
    """A label for the artifact's job (one artifact per matrix leg is usual)."""
    return "github-actions/" + re.sub(r"[^A-Za-z0-9._:+@-]", "-", artifact)[:85]


def _historical(run: WorkflowRun, artifact: RunArtifact) -> HistoricalReport:
    return HistoricalReport(
        ci_run_id=str(run.id),
        ci_attempt=run.attempt,
        job_name=artifact.name,
        commit_sha=run.head_sha,
        ref=f"refs/heads/{run.head_branch}" if run.head_branch else "",
        environment=_environment(artifact.name),
        started_at=run.started_at,
        completed_at=run.completed_at,
        run_url=run.url,
    )


async def backfill_ci_history(
    engine: AsyncEngine,
    *,
    history: GitHubActionsHistory,
    backfill: Backfill,
    org_id: str,
    repo_id: str,
    max_runs: int,
    workflow: str | None,
    branch: str | None,
    pattern: str,
) -> dict[str, Any]:
    """Resolve the pair's binding, then import each matching artifact once.
    Archive refusals (download, unsafe zip) are audited like parse refusals."""
    async with transaction(engine) as session:
        binding = await SqlRepoDirectory(session).get(repo_id)
    if binding is None or binding.org_id != org_id:
        raise ResourceNotFound
    owner, name = binding.owner, binding.name
    origin = Origin(Channel.CLI, str(uuid.uuid4()))
    runs = await history.completed_runs(
        owner=owner, name=name, limit=max_runs, workflow=workflow, branch=branch
    )
    rows: list[dict[str, Any]] = []
    for run in runs:
        if len(rows) >= MAX_BACKFILL_ARTIFACTS:
            break
        found = await history.artifacts(owner=owner, name=name, run_id=run.id)
        matching = [a for a in found if fnmatch.fnmatchcase(a.name, pattern)]
        room = MAX_BACKFILL_ARTIFACTS - len(rows)
        for artifact in matching[: min(MAX_ARTIFACTS_PER_RUN, room)]:
            report = _historical(run, artifact)
            result: BackfillResult
            try:
                archive = await history.download(
                    owner=owner, name=name, artifact=artifact
                )
                documents = await asyncio.to_thread(extract_reports, archive)
            except (CodeHostError, ReportRejected) as exc:
                result = await backfill.refuse(
                    org_id=org_id,
                    repo_id=repo_id,
                    report=report,
                    reason=exc.reason,
                    origin=origin,
                )
            else:
                result = await backfill.import_report(
                    org_id=org_id,
                    repo_id=repo_id,
                    report=report,
                    archive=archive,
                    documents=documents,
                    origin=origin,
                )
            rows.append(
                {
                    "ci_run_id": report.ci_run_id,
                    "ci_attempt": report.ci_attempt,
                    "artifact": artifact.name,
                    "ci_completed_at": report.completed_at.isoformat(),
                    "status": result.status.value,
                    "reason": result.reason,
                    "run_id": result.run_id,
                    "tests": result.tests,
                }
            )
    return {
        "org_id": org_id,
        "repo_id": repo_id,
        "source": "github_actions_backfill",
        "runs_read": len(runs),
        "totals": {
            status.value: sum(1 for row in rows if row["status"] == status.value)
            for status in BackfillStatus
        },
        "artifacts": rows,
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
<style>{OFFLINE_REPORT_CSS}</style>
</head>
<body class="report-body">
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
<table class="data-table">{metadata_rows}</table>
<p class="note">The raw report body is stored unparsed at the artifact key above
(a plaintext file; encryption is not wired in this MVP).</p>

<h2>Tests, attempts, and classification</h2>
<table class="data-table">
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
