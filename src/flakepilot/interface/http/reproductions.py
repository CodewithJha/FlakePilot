"""Schedule and inspect one reproduction.

`POST /orgs/{org_id}/reproductions` with a session cookie. The org is the URL,
checked against the session. The body is `repo_id`, `selector` and `sha` only.
Closed mode (the default) stores `inconclusive` / `execution_closed` and does
not fetch source, enqueue a job, or start a runtime. A repeat of that tuple
returns the same id.

`GET /orgs/{org_id}/reproductions/{execution_id}` returns state and artifact
names. It does not return artifact bytes.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from flakepilot.execution.application.reproduction import (
    EvidenceRef,
    Execution,
    evidence_of,
)
from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.interface.composition_root.reproduction import Reproductions
from flakepilot.interface.http.auth import org_access, request_origin
from flakepilot.shared_kernel.events import Actor, ActorType

router = APIRouter(tags=["reproductions"])


class ReproductionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: UUID
    selector: str = Field(min_length=1, max_length=120)
    sha: str = Field(pattern=r"[0-9a-f]{40}")


def _service(request: Request) -> Reproductions:
    service: Reproductions | None = getattr(request.app.state, "reproductions", None)
    if service is None:
        raise RuntimeError("reproductions are not configured")
    return service


def _public(record: Execution, artifacts: tuple[EvidenceRef, ...]) -> dict[str, object]:
    verdict = evidence_of(record)
    return {
        "id": record.id,
        "state": record.state,
        "outcome": record.outcome,
        "reason": record.reason,
        "evidence_partial": record.evidence_partial,
        "evidence_status": verdict.status,
        "evidence_reason": verdict.reason,
        "selector": record.selector,
        "sha": record.sha,
        "artifacts": [
            {"kind": item.kind, "key": item.key, "size": item.size}
            for item in artifacts
        ],
    }


@router.post("/orgs/{org_id}/reproductions", response_model=None)
async def create_reproduction(
    org_id: UUID,
    body: ReproductionBody,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> JSONResponse:
    service = _service(request)
    record, created = await service.request(
        access=access,
        org_id=str(org_id),
        repo_id=str(body.repo_id),
        selector=body.selector,
        sha=body.sha,
        actor=Actor(ActorType.USER, access.principal_id),
        origin=request_origin(),
        now=datetime.now(UTC),
    )
    _loaded, artifacts = await service.load(
        access=access, org_id=str(org_id), execution_id=record.id
    )
    return JSONResponse(_public(record, artifacts), 201 if created else 200)


@router.get("/orgs/{org_id}/reproductions/{execution_id}", response_model=None)
async def read_reproduction(
    org_id: UUID,
    execution_id: UUID,
    request: Request,
    access: Annotated[OrgAccess, Depends(org_access)],
) -> JSONResponse:
    record, artifacts = await _service(request).load(
        access=access, org_id=str(org_id), execution_id=str(execution_id)
    )
    return JSONResponse(_public(record, artifacts))
