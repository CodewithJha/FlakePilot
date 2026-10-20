"""Health probes.

- Liveness (`/health/live`): the process can serve HTTP. It never checks dependencies,
  so a dependency outage never gets the process restarted.
- Readiness (`/health/ready`): 200 only between lifespan startup completing and lifespan
  shutdown starting; 503 otherwise. Settings are validated before the app exists.
  The server drains connections before lifespan shutdown, so readiness does not flip during
  the drain; revisit with a drain signal when a load balancer or orchestrator is introduced.
  Dependency checks (PostgreSQL) are added with persistence in Stage 2.3 step 3.2.

Bodies carry only a status field.
"""

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok", "ready", "not_ready"]


@router.get("/live")
async def live() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/ready", responses={503: {"model": HealthStatus}})
async def ready(request: Request, response: Response) -> HealthStatus:
    if request.app.state.ready:
        return HealthStatus(status="ready")
    response.status_code = 503
    return HealthStatus(status="not_ready")
