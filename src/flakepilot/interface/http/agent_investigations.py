"""Investigation for bound AI agents (goal 9): `POST /agent/orgs/{org_id}/runs/{run_id}/investigation`.

Bearer only: `Authorization: Bearer <Auth0 access token>`. Cookies are never
read here, so there is no ambient credential and no CSRF surface; a request
that carries a session cookie gets nothing from it. The router is mounted only
when an Auth0 issuer is configured, so an unconfigured deployment answers this
path exactly like an unknown route (404).

Answers mirror the session route: a missing or refused token is 401
`unauthenticated` (no reason); no active binding for the token's subject in
this org, a revoked or expired binding, a run outside the bound repository, an
unknown run, and an unverified delegator standing are all the same 404 body; a
visible run without `repo.investigate` in the token or delegation is 403. The
200 body is the session route's JSON (no audio route exists for agents).
"""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.interface.composition_root.agent_investigations import (
    AgentInvestigations,
)
from flakepilot.interface.http.auth import request_origin
from flakepilot.interface.http.errors import Unauthenticated
from flakepilot.interface.http.investigations import NO_STORE, _body

router = APIRouter(tags=["agents"])


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated
    return token.strip()


async def agent_investigate(
    org_id: UUID, run_id: UUID, request: Request
) -> JSONResponse:
    agents: AgentInvestigations | None = getattr(
        request.app.state, "agent_investigations", None
    )
    if agents is None:
        raise ResourceNotFound
    report = await agents.investigate(
        bearer=_bearer(request),
        org_id=str(org_id),
        run_id=str(run_id),
        origin=request_origin(),
    )
    # Without a configured origin the link echoes this request's own base URL
    # back to the same caller; it is never stored or spoken.
    public_origin: str | None = getattr(request.app.state, "public_origin", None)
    origin = public_origin or str(request.base_url).rstrip("/")
    report_url = f"{origin}/orgs/{org_id}/runs/{run_id}"
    return JSONResponse(_body(report, report_url), headers=NO_STORE)


router.add_api_route(
    "/agent/orgs/{org_id}/runs/{run_id}/investigation",
    agent_investigate,
    methods=["POST"],
    status_code=200,
    summary="Run the bounded investigation of one stored run as a bound agent",
)
