"""One real Auth0 token through the agent investigation route (goal 9). Owner-run only.

Skipped unless the Auth0 variables and FLAKEPILOT_TEST_DATABASE_URL are set, so
the default suite and CI contact no tenant. A skip is not verification. The
test requests exactly one client-credentials token, binds that client's
subject (`<client_id>@clients`) in a throwaway migrated database, and calls
`POST /agent/orgs/{org}/runs/{run}/investigation` in-process through the real
`create_app` lifespan with the tenant's real key set. No hosted provider is
configured, and the code-host standing is a `verified_read` stand-in: no
GitHub App installation exists for this deployment.

    FLAKEPILOT_TEST_DATABASE_URL=... FLAKEPILOT_AUTH0_DOMAIN=... \\
    FLAKEPILOT_AUTH0_AUDIENCE=... FLAKEPILOT_LIVE_AUTH0_CLIENT_ID=... \\
    FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET=... uv run pytest tests/live -k auth0_route

The token and secret are never logged or asserted on directly.
"""

import asyncio
import json
import os
import urllib.request
from datetime import timedelta
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, seed
from auth0_support import tampered
from sqlalchemy import text
from support import send_raw, throwaway_database

from flakepilot.integrations.infrastructure.hosted_http import https_transport
from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.composition_root.agent_binding_commands import bind, unbind
from flakepilot.interface.composition_root.database import create_engine, transaction

DOMAIN = os.environ.get("FLAKEPILOT_AUTH0_DOMAIN", "")
AUDIENCE = os.environ.get("FLAKEPILOT_AUTH0_AUDIENCE", "")
CLIENT_ID = os.environ.get("FLAKEPILOT_LIVE_AUTH0_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET", "")


def _client_credentials_token() -> str:
    request = urllib.request.Request(
        f"https://{DOMAIN}/oauth/token",
        data=json.dumps(
            {
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "audience": AUDIENCE,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    status, body = https_transport(request, 10.0, 64 * 1024)
    assert status == 200, f"token endpoint answered HTTP {status}"
    token = json.loads(body).get("access_token")
    assert isinstance(token, str), "no access_token in the answer"
    return token


async def _scenario(url: str, token: str) -> dict[str, Any]:
    settings = Settings(Secret(url), auth0_domain=DOMAIN, auth0_audience=AUDIENCE)
    engine = create_engine(url)
    try:
        seeded = await seed(engine)
        subject = f"{CLIENT_ID}@clients"
        await bind(
            settings,
            org_id=seeded.org_id,
            github_id=ADMIN_GITHUB_ID,
            repo_id=seeded.repo_id,
            subject=subject,
            agent_name="live_agent",
            scopes=["repo.view", "repo.investigate"],
            ttl=timedelta(minutes=10),
        )
        app = create_app(settings, access_verifier=lambda _e, _s: ReadVerifier())
        target = f"/agent/orgs/{seeded.org_id}/runs/{seeded.run_id}/investigation"
        bearer = [("authorization", f"Bearer {token}")]
        async with app.router.lifespan_context(app):
            granted = await send_raw(app, "POST", target, bearer)
            forged = await send_raw(
                app, "POST", target, [("authorization", f"Bearer {tampered(token)}")]
            )
            await unbind(
                settings,
                org_id=seeded.org_id,
                github_id=ADMIN_GITHUB_ID,
                subject=subject,
            )
            revoked = await send_raw(app, "POST", target, bearer)
        async with transaction(engine) as session:
            rows = list(
                await session.execute(
                    text(
                        "SELECT actor_id, payload->>'via_agent' AS via, after "
                        "FROM diag.outbox WHERE org_id = :org "
                        "AND name = 'diag.investigation.recorded'"
                    ),
                    {"org": seeded.org_id},
                )
            )
        return {
            "seeded": seeded,
            "granted": granted,
            "forged": forged[0],
            "revoked": revoked[0],
            "rows": rows,
        }
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not (
        DOMAIN
        and AUDIENCE
        and CLIENT_ID
        and CLIENT_SECRET
        and os.environ.get("FLAKEPILOT_TEST_DATABASE_URL")
    ),
    reason="Auth0 live variables or FLAKEPILOT_TEST_DATABASE_URL are not set",
)
def test_live_auth0_token_reaches_the_agent_route_and_only_while_bound() -> None:
    token = _client_credentials_token()
    with throwaway_database(migrate=True) as url:
        result = asyncio.run(_scenario(url, token))
    status, _, body = result["granted"]
    assert status == 200
    answer = json.loads(body)
    assert answer["stopping"] == "complete"
    assert answer["run_id"] == result["seeded"].run_id
    [row] = result["rows"]
    assert str(row.actor_id) == result["seeded"].admin_id
    assert (row.via, row.after) == ("live_agent", "complete")
    assert result["forged"] == 401
    assert result["revoked"] == 404
