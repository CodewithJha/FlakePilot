"""Health probes.

- Liveness (`/health/live`): the process can serve HTTP. It never checks dependencies,
  so a dependency outage never gets the process restarted.
- Readiness (`/health/ready`): 200 only when lifespan startup has completed, shutdown has
  not begun, and the injected readiness probe (a bounded database check) succeeds; 503
  otherwise. Settings are validated before the app exists.
  The server drains connections before lifespan shutdown, so readiness does not flip during
  the drain; revisit with a drain signal when a load balancer or orchestrator is introduced.

Bodies carry only a status field.
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

ReadinessProbe = Callable[[], Awaitable[bool]]

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok", "ready", "not_ready"]


@router.get("/live")
async def live() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/ready", responses={503: {"model": HealthStatus}})
async def ready(request: Request, response: Response) -> HealthStatus:
    state = request.app.state
    probe: ReadinessProbe | None = getattr(state, "readiness_probe", None)
    if state.ready and probe is not None and await probe():
        return HealthStatus(status="ready")
    response.status_code = 503
    return HealthStatus(status="not_ready")
