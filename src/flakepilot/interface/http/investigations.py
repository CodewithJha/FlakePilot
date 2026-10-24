"""Investigation for signed-in users (ADR-017 goals 2, 3, 8, 14, 15).

`POST /orgs/{org_id}/runs/{run_id}/investigation` runs DIA's bounded
investigation of one stored run and answers JSON: stage records, research
sources, captured page hashes, the assist's counts, the history ranking, and
the counts-only brief text. `POST .../investigation/brief` runs it again and
answers the brief as audio, only when a speech provider is configured.

Both need a session (and, being POSTs, a same-origin request: CSRF), then
`repo.investigate` on the run's repo with a verified code-host standing. The
chain is the run view's: a run outside the caller's scope, an unverified
standing, and a missing grant on an invisible repo are the same 404; a visible
repo without the grant is 403. Every call records provenance. Nothing here
executes code, changes a verdict, or reveals hypothesis text; model claims
stay in the diagnosis store as counts. Providers that are not configured are
skipped stages, never errors.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from flakepilot.diagnosis.application.brief import compose_brief
from flakepilot.diagnosis.application.investigation import InvestigationReport
from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.interface.composition_root.investigations import Investigations
from flakepilot.interface.http.auth import org_access, request_origin

router = APIRouter(tags=["investigations"])

NO_STORE = {"Cache-Control": "no-store"}


def _report_url(request: Request, org_id: UUID, run_id: UUID) -> str:
    # The session dependency already proved auth_config (and its origin) exists.
    return f"{request.app.state.auth_config.public_origin}/orgs/{org_id}/runs/{run_id}"


def _body(report: InvestigationReport, report_url: str) -> dict[str, Any]:
    assist = report.assist
    ranked = report.prioritization.ranked if report.prioritization else ()
    return {
        "org_id": report.org_id,
        "repo_id": report.repo_id,
        "run_id": report.run_id,
        "stopping": report.stopping.value,
        "stages": [
            {"name": s.name.value, "status": s.status.value, "reason": s.reason}
            for s in report.stages
        ],
        "sources": [
            {
                "url": s.url,
                "title": s.title,
                "retrieved_at": s.retrieved_at.isoformat(),
            }
            for s in report.sources
        ],
        "pages": [
            {
                "url": p.url,
                "sha256": p.sha256,
                "size": p.size,
                "retrieved_at": p.retrieved_at.isoformat(),
            }
            for p in report.pages
        ],
        "assist": None
        if assist is None
        else {
            "request_id": assist.request_id,
            "outcome": assist.outcome.value,
            "reason": assist.reason,
            "linked": assist.linked,
            "unverified": assist.unverified,
            "contradicted": assist.contradicted,
        },
        "prioritized": [
            {
                "rank": p.rank,
                "test_id": p.test_id,
                "flaky_runs": p.flaky_runs,
                "failed_attempts": p.failed_attempts,
            }
            for p in ranked
        ],
        "brief": compose_brief(report, report_url=report_url),
    }


async def investigate(
    org_id: UUID,
    run_id: UUID,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> JSONResponse:
    investigations: Investigations = request.app.state.investigations
    report = await investigations.investigate(
        access=access, org_id=str(org_id), run_id=str(run_id), origin=request_origin()
    )
    return JSONResponse(
        _body(report, _report_url(request, org_id, run_id)), headers=NO_STORE
    )


router.add_api_route(
    "/orgs/{org_id}/runs/{run_id}/investigation",
    investigate,
    methods=["POST"],
    status_code=200,
    summary="Run the bounded investigation of one stored run",
)


async def brief_audio(
    org_id: UUID,
    run_id: UUID,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> Response:
    investigations: Investigations = request.app.state.investigations
    audio, media_type = await investigations.brief_audio(
        access=access,
        org_id=str(org_id),
        run_id=str(run_id),
        origin=request_origin(),
        report_url=_report_url(request, org_id, run_id),
    )
    return Response(audio, media_type=media_type, headers=NO_STORE)


router.add_api_route(
    "/orgs/{org_id}/runs/{run_id}/investigation/brief",
    brief_audio,
    methods=["POST"],
    status_code=200,
    summary="Investigate one stored run and speak the counts-only brief",
    response_class=Response,
)
