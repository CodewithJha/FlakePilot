"""The landing page (`GET /`): the one page a first-time visitor of the public
origin sees before any authentication.

It stays unauthed and database-free. The composition root builds the app
identically for every deployment, and this page needs only the sign-in route
the `auth` router already mounts plus the built React shell. The copy keeps
the product's exact claims: ingest CI test reports, classify failures with
the deterministic rule set, and the control plane never executes repository
code. The schematic in the client shows the run view's shape with explicitly
empty verdict cells, not sample results.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from flakepilot.interface.http.shell import document
from flakepilot.interface.http.ui import HTML_SECURITY_HEADERS

router = APIRouter(tags=["landing"])

_NOSCRIPT = (
    "<p>FlakePilot reads stored CI evidence. This page draws with JavaScript "
    "from this origin.</p>"
    '<p><a href="/auth/github/login">Sign in with GitHub</a></p>'
)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing() -> HTMLResponse:
    return HTMLResponse(
        document(
            title="FlakePilot — flaky tests, read like evidence",
            boot={"kind": "landing"},
            noscript=_NOSCRIPT,
        ),
        status_code=200,
        headers={**HTML_SECURITY_HEADERS, "Cache-Control": "private, no-store"},
    )
