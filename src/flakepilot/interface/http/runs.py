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
IDs, SHAs, evidence values — is HTML-escaped, including the values inside the
classification evidence. Escaping is the first line of defense; these
responses also carry a strict browser-side policy (they reference no
external resource and are never framed), and the middleware adds
`nosniff`/`Referrer-Policy` to every response.
"""

import html
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

# The pages' own policy: no external resource may load, no script may run
# (default-src 'none' denies script-src and every other fallback too), the
# only styles are the two inline <style> blocks, and the pages are never
# framed (frame-ancestors 'none', plus X-Frame-Options for older browsers).
HTML_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
}

from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.interface.composition_root.reporting import (
    classification_cell,
    fail_cascade,
)
from flakepilot.interface.composition_root.run_views import (
    ClassifiedRunReport,
    RunIndexReport,
)
from flakepilot.interface.http.auth import org_access

router = APIRouter(tags=["runs"])


def _e(value: object) -> str:
    return html.escape(str(value))


LIMIT_QUERY = 50
"""The listing page's default; the use case clamps it. The limit is a query
parameter, never an authorization boundary: what is listed is what
`repo.view` already covers."""


def _index_page(index: RunIndexReport) -> str:
    rows = "\n".join(
        "<tr>"
        f'<td><a href="/orgs/{_e(index.org_id)}/runs/{_e(entry.run_id)}">{_e(entry.run_id[:8])}</a></td>'
        f"<td><code>{_e(entry.commit_sha[:12])}</code></td>"
        f"<td>{_e(entry.environment)}</td>"
        f"<td>{_e(entry.source)}</td>"
        f"<td>{_e(entry.revision_status)}</td>"
        f"<td>{fail_cascade(entry.received_at)}</td>"
        f"<td>{entry.summary.passed}/{entry.summary.tests} passed"
        f" &middot; {entry.summary.failed} failed &middot;"
        f" {entry.summary.skipped} skipped</td>"
        "</tr>"
        for entry in index.entries
    )
    body = (
        f"""<table>
<tr><th>Run</th><th>Commit</th><th>Environment</th><th>Source</th><th>Revision</th><th>Received</th><th>Summary</th></tr>
{rows}
</table>"""
        if index.entries
        else "<p>No stored runs for this repository yet. Runs appear here"
        " when a CI upload with a token for this repository is ingested."
        " There may be runs you cannot see; what you cannot see is not"
        " listed.</p>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlakePilot runs {_e(index.repo_id[:8])}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; color: #1a1a2e; }}
  h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d5d5dc; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f4f4f8; font-weight: 600; }}
  td code {{ font-size: 0.8rem; word-break: break-all; }}
  .note {{ color: #555; font-size: 0.8rem; }}
  .muted {{ color: #777; }}
</style>
</head>
<body>
<h1>FlakePilot runs</h1>
<p class="muted">
Repo <code>{_e(index.repo_id)}</code> &middot; org <code>{_e(index.org_id)}</code>
&middot; showing at most {len(index.entries)} stored run(s), newest first
</p>
<p>This page lists the stored CI runs for one repository in your
organization. Nothing is executed here and nothing is verified here: the
commit SHAs are what the uploads reported, and each run's revision status
stays <code>unchecked</code> until a real revision check has happened. Per-test
verdicts live on each run's own view; a link here opens only that run's
read-only view.</p>
{body}
<p class="note">Classification verdicts per test come from the deterministic,
versioned rule set <code>ti-classification/1</code> on each run's view. This
listing never claims code-host verification, revision ancestry, or execution.</p>
</body>
</html>
"""


def _page(report: ClassifiedRunReport) -> str:
    run = report.run
    metadata = run.metadata
    rows = "\n".join(
        "<tr>"
        f"<td>{_e(case.case.classname)}</td>"
        f"<td>{_e(case.case.name)}</td>"
        f"<td>{' &rarr; '.join(_e(attempt) for attempt in case.case.attempts)}</td>"
        f"<td>{classification_cell(case)}</td>"
        "</tr>"
        for case in report.cases
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlakePilot run {_e(run.run_id[:8])}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; color: #1a1a2e; }}
  h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d5d5dc; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f4f4f8; font-weight: 600; }}
  td code {{ font-size: 0.8rem; word-break: break-all; }}
  .note {{ color: #555; font-size: 0.8rem; }}
  .muted {{ color: #777; }}
</style>
</head>
<body>
<h1>FlakePilot run view</h1>
<p class="muted">
Run <code>{_e(run.run_id)}</code> &middot; org <code>{_e(run.org_id)}</code>
&middot; repo <code>{_e(run.repo_id)}</code>
&middot; commit <code>{_e(run.commit_sha)}</code>
&middot; environment <code>{_e(run.environment)}</code>
&middot; source <code>{_e(run.source)}</code>
</p>
<p>This page is a read-only view of what one CI upload stored for your
organization. Nothing is executed here. The commit SHA is what the upload
reported; the revision status is <code>{_e(run.revision_status)}</code> — no
code-host verification is available yet. The classification column is the
deterministic, versioned rule set <code>ti-classification/1</code> applied to
the stored evidence (no confidence scores, no model output);
&ldquo;inconclusive&rdquo; is a real verdict where the stored evidence cannot
support a stronger one.</p>

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
<table>
<tr><th>CI run ID</th><td>{fail_cascade(metadata.ci_run_id if metadata else None)}</td></tr>
<tr><th>CI attempt</th><td>{fail_cascade(metadata.ci_attempt if metadata else None)}</td></tr>
<tr><th>Job name</th><td>{fail_cascade(metadata.job_name if metadata else None)}</td></tr>
<tr><th>Ref</th><td>{fail_cascade(metadata.ref if metadata else None)}</td></tr>
<tr><th>Environment</th><td>{fail_cascade(metadata.environment if metadata else run.environment)}</td></tr>
<tr><th>Started at</th><td>{fail_cascade(metadata.started_at if metadata else None)}</td></tr>
<tr><th>Finished at</th><td>{fail_cascade(metadata.finished_at if metadata else None)}</td></tr>
<tr><th>Received</th><td>{fail_cascade(run.received_at)}</td></tr>
</table>

<h2>Tests, attempts, and classification</h2>
<table>
<tr><th>Classname</th><th>Test name</th><th>Attempts (chronological)</th><th>Classification</th></tr>
{rows}
</table>
<p class="note">An attempt chain like &ldquo;failed &rarr; passed&rdquo; is a
recurring failure within the CI run itself. Only CI-report evidence counts;
FlakePilot's own runs are excluded and listed as <code>excluded_runs</code>.
The stored report body is kept unparsed as a plaintext file; it is not
downloaded from here.</p>
</body>
</html>
"""


async def run_view(
    org_id: UUID,
    run_id: str,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> HTMLResponse:
    report = await request.app.state.run_views.classified(
        access=access, org_id=str(org_id), run_id=run_id
    )
    return HTMLResponse(_page(report), status_code=200, headers=HTML_SECURITY_HEADERS)


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
    return HTMLResponse(
        _index_page(report), status_code=200, headers=HTML_SECURITY_HEADERS
    )


router.add_api_route(
    "/orgs/{org_id}/repos/{repo_id}/runs",
    runs_index,
    methods=["GET"],
    status_code=200,
    summary="Read-only listing of one repository's stored runs",
)
