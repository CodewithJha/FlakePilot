"""Server-rendered run views for signed-in users.

`GET /orgs/{org_id}/runs/{run_id}` and `GET
/orgs/{org_id}/repos/{repo_id}/runs` with a session cookie: read-only views
of the stored CI evidence — one run's attempt chains and per-test
classification from the deterministic rule set `ti-classification/1`, and the
listing of one repository's stored runs. They state plainly what they are
not: not a claim of code-host verification, and no execution of anything.

The org and repo in the URL are only lookup keys. Authorization is
`org_access` (IAM's records for the verified session principal; not a member
is not found) and then the use cases' authorization-checked reads, which
mirror the shipped execution views: a resource outside the caller's scope is
not found, so a cross-org request learns nothing, and viewing needs
`repo.view` exactly as every other session read does.

Every user-controlled string — test names, classnames, attempt outcomes, run
IDs, SHAs, evidence values — is placed in a JSON boot payload with `<` and
`>` escaped, so a stored value cannot close the element or become markup.
These responses also carry a strict browser-side policy (script, style and
font files are same-origin; nothing else is loaded; they are never framed),
and the middleware adds `nosniff`/`Referrer-Policy` to every response.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.interface.composition_root.run_views import (
    ClassifiedRunReport,
    RunIndexReport,
)
from flakepilot.interface.http.auth import org_access
from flakepilot.interface.http.shell import document
from flakepilot.interface.http.ui import HTML_SECURITY_HEADERS

router = APIRouter(tags=["runs"])


_HTML_HEADERS = {**HTML_SECURITY_HEADERS, "Cache-Control": "private, no-store"}

_NOSCRIPT = (
    "<p>This read-only view draws with JavaScript from this origin. "
    "Nothing is executed here.</p>"
)

LIMIT_QUERY = 50
"""The listing page's default; the use case clamps it. The limit is a query
parameter, never an authorization boundary: what is listed is what
`repo.view` already covers."""


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _evidence(classification: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for key in sorted(classification.evidence):
        value = classification.evidence[key]
        if value is None or value == [] or value == "":
            continue
        rendered = (
            ", ".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        items.append({"key": str(key), "value": rendered})
    return items


def _index_page(index: RunIndexReport) -> str:
    entries = [
        {
            "href": f"/orgs/{index.org_id}/runs/{entry.run_id}",
            "run_id": entry.run_id,
            "commit_sha": entry.commit_sha,
            "environment": entry.environment,
            "source": entry.source,
            "revision_status": entry.revision_status,
            "received_at": _iso(entry.received_at),
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
    ]
    boot: dict[str, Any] = {
        "kind": "index",
        "heading": "FlakePilot runs",
        "org_id": index.org_id,
        "repo_id": index.repo_id,
        "shown": len(index.entries),
        "lead": (
            "This page lists the stored CI runs for one repository in your "
            "organization. Nothing is executed here and nothing is verified "
            "here: the commit SHAs are what the uploads reported, and each "
            "run's revision status stays unchecked until a real revision "
            "check has happened. Per-test verdicts live on each run's own "
            "view; a link here opens only that run's read-only view. Rows "
            "are newest first."
        ),
        "empty": (
            None
            if index.entries
            else (
                "No stored runs for this repository yet. Runs appear here "
                "when a CI upload with a token for this repository is "
                "ingested. There may be runs you cannot see; what you cannot "
                "see is not listed."
            )
        ),
        "footnote": (
            "Classification verdicts per test come from the deterministic, "
            "versioned rule set ti-classification/1 on each run's view. This "
            "listing never claims code-host verification, revision ancestry, "
            "or execution."
        ),
        "entries": entries,
    }
    return document(
        title="FlakePilot runs",
        boot=boot,
        noscript=_NOSCRIPT,
    )


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _case_payload(case: Any) -> dict[str, Any]:
    classification = case.classification
    if classification is None:
        return {
            "classname": case.case.classname,
            "name": case.case.name,
            "attempts": list(case.case.attempts),
            "classification": None,
            "label": "classification unavailable",
        }
    return {
        "classname": case.case.classname,
        "name": case.case.name,
        "attempts": list(case.case.attempts),
        "classification": {
            "failure_class": classification.failure_class.value,
            "reason": (classification.reason.value if classification.reason else None),
            "target_sha": classification.target_sha,
            "rule_version": classification.rule_version,
            "evidence": _evidence(classification),
        },
        "label": classification.failure_class.value,
    }


def _page(report: ClassifiedRunReport) -> str:
    run = report.run
    metadata = run.metadata
    upload = [
        ("CI run ID", metadata.ci_run_id if metadata else None),
        ("CI attempt", metadata.ci_attempt if metadata else None),
        ("Job name", metadata.job_name if metadata else None),
        ("Ref", metadata.ref if metadata else None),
        ("Environment", metadata.environment if metadata else run.environment),
        ("Started at", metadata.started_at if metadata else None),
        ("Finished at", metadata.finished_at if metadata else None),
        ("Received", run.received_at),
    ]
    boot: dict[str, Any] = {
        "kind": "run",
        "heading": "FlakePilot run view",
        "disclaimer": (
            "This page is a read-only view of what one CI upload stored for "
            "your organization. Nothing is executed here. The commit SHA is "
            "what the upload reported; the revision status is "
            f"{run.revision_status} — no code-host verification is available "
            "yet. The classification is applied by rule ti-classification/1 "
            "to the stored evidence (no confidence scores, no model output); "
            '"inconclusive" is a real verdict where the stored evidence '
            "cannot support a stronger one."
        ),
        "run_id": run.run_id,
        "org_id": run.org_id,
        "repo_id": run.repo_id,
        "commit_sha": run.commit_sha,
        "environment": run.environment,
        "source": run.source,
        "revision_status": run.revision_status,
        "summary": {
            "tests": run.summary.tests,
            "attempts": run.summary.attempts,
            "passed": run.summary.passed,
            "failed": run.summary.failed,
            "errored": run.summary.errored,
            "skipped": run.summary.skipped,
        },
        "upload": [{"label": label, "value": _text(value)} for label, value in upload],
        "cases": [_case_payload(case) for case in report.cases],
        "footnote": (
            'An attempt chain like "failed → passed" is a recurring failure '
            "within the CI run itself. Only CI-report evidence counts; "
            "FlakePilot's own runs are excluded and listed as excluded_runs. "
            "The stored report body is kept unparsed as a plaintext file; it "
            "is not downloaded from here."
        ),
    }
    return document(title="FlakePilot run view", boot=boot, noscript=_NOSCRIPT)


async def run_view(
    org_id: UUID,
    run_id: str,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> HTMLResponse:
    report = await request.app.state.run_views.classified(
        access=access, org_id=str(org_id), run_id=run_id
    )
    return HTMLResponse(_page(report), status_code=200, headers=_HTML_HEADERS)


router.add_api_route(
    "/orgs/{org_id}/runs/{run_id}",
    run_view,
    methods=["GET"],
    status_code=200,
    summary="Read-only run view for the signed-in user",
)


def _classification_payload(report: ClassifiedRunReport) -> dict[str, Any]:
    """The run's `ti-classification/1` verdicts as JSON data.

    Exactly the domain verdicts the HTML view renders and the operator CLI
    prints: rule version, failure class, reason, target revision and evidence
    per case, plus the run summary and commit SHA. No artifact key, no raw
    report body, no upload metadata beyond the run's own identity. Every
    stored string is data, not markup — the JSON encoder escapes nothing and
    needs no escaping.
    """

    def verdict(case: Any) -> dict[str, Any]:
        classification = case.classification
        if classification is None:
            return {
                "classname": case.case.classname,
                "name": case.case.name,
                "attempts": list(case.case.attempts),
                "classification": None,
            }
        return {
            "classname": case.case.classname,
            "name": case.case.name,
            "attempts": list(case.case.attempts),
            "classification": {
                "failure_class": classification.failure_class.value,
                "reason": (
                    classification.reason.value if classification.reason else None
                ),
                "target_sha": classification.target_sha,
                "rule_version": classification.rule_version,
                "evidence": dict(classification.evidence),
            },
        }

    return {
        "schema": "flakepilot.classification/1",
        "rule_version": "ti-classification/1",
        "run_id": report.run.run_id,
        "org_id": report.run.org_id,
        "repo_id": report.run.repo_id,
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
        "cases": [verdict(case) for case in report.cases],
    }


async def run_classification_json(
    org_id: UUID,
    run_id: str,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> JSONResponse:
    """`ti-classification/1` verdicts as data, for the signed-in user.

    The authorization chain is the HTML run view's own (session -> IAM's
    records -> verified code-host standing on this pair -> `repo.view`); a
    denial is the same non-disclosing 404. The response is a JSON document —
    machine-readable classification for tooling — with the same content the
    HTML view shows and nothing more.
    """
    report = await request.app.state.run_views.classification_json(
        access=access, org_id=str(org_id), run_id=run_id
    )
    return JSONResponse(_classification_payload(report))


router.add_api_route(
    "/orgs/{org_id}/runs/{run_id}/classification",
    run_classification_json,
    methods=["GET"],
    status_code=200,
    summary="The run's ti-classification/1 verdicts as JSON",
)


async def runs_index(
    org_id: UUID,
    repo_id: str,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
    limit: int = LIMIT_QUERY,
) -> HTMLResponse:
    """One repository's stored runs, newest first, for the signed-in user.

    `repo_id` in the URL is only a lookup key, exactly like the org: a
    non-canonical value is the same 404 any unknown pair answers (decided in
    the use case, no store read), and the authorization chain is the run
    view's own: session -> IAM's records -> verified code-host standing on
    this exact pair -> `repo.view`.
    """
    report = await request.app.state.run_views.runs_index(
        access=access, org_id=str(org_id), repo_id=repo_id, limit=limit
    )
    return HTMLResponse(_index_page(report), status_code=200, headers=_HTML_HEADERS)


router.add_api_route(
    "/orgs/{org_id}/repos/{repo_id}/runs",
    runs_index,
    methods=["GET"],
    status_code=200,
    summary="Read-only listing of one repository's stored runs",
)
