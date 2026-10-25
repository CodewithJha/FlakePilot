"""One real Auth0 client-credentials token through the verifier (goal 9). Owner-run only.

Skipped unless all four variables are set, so the default suite and CI contact
no tenant. A skip is not verification. Running it requests exactly one token
for an M2M application authorized for `repo.view repo.investigate`; the
refusal checks reuse that token and only refetch the tenant's public key set:

    FLAKEPILOT_AUTH0_DOMAIN=... FLAKEPILOT_AUTH0_AUDIENCE=... \\
    FLAKEPILOT_LIVE_AUTH0_CLIENT_ID=... FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET=... \\
        uv run pytest tests/live -k auth0

The token and secret are never logged or asserted on directly.
"""

import asyncio
import json
import os
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from flakepilot.identity.application.agent_tokens import narrow
from flakepilot.identity.application.agents import AgentAccess
from flakepilot.identity.application.authorization import Grant
from flakepilot.integrations.infrastructure.auth0 import (
    Auth0TokenVerifier,
    TokenRejected,
)
from flakepilot.integrations.infrastructure.hosted_http import https_transport

DOMAIN = os.environ.get("FLAKEPILOT_AUTH0_DOMAIN", "")
AUDIENCE = os.environ.get("FLAKEPILOT_AUTH0_AUDIENCE", "")
CLIENT_ID = os.environ.get("FLAKEPILOT_LIVE_AUTH0_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET", "")
REPO = "repo_1"


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


def _delegation(*grants: Grant) -> AgentAccess:
    return AgentAccess(
        principal_id="user_1",
        org_id="org_1",
        grants=frozenset((REPO, grant) for grant in grants),
        agent_id="triage_bot",
        repo_id=REPO,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _rejected(verifier: Auth0TokenVerifier, token: str) -> bool:
    try:
        asyncio.run(verifier.verify(token))
    except TokenRejected:
        return True
    return False


@pytest.mark.skipif(
    not (DOMAIN and AUDIENCE and CLIENT_ID and CLIENT_SECRET),
    reason="FLAKEPILOT_AUTH0_DOMAIN/_AUDIENCE or FLAKEPILOT_LIVE_AUTH0_CLIENT_ID/"
    "_SECRET is not set",
)
def test_live_auth0_token_verifies_and_only_narrows() -> None:
    token = _client_credentials_token()
    verified = asyncio.run(
        Auth0TokenVerifier(domain=DOMAIN, audience=AUDIENCE).verify(token)
    )
    now = datetime.now(UTC)
    assert verified.scopes == {"repo.view", "repo.investigate"}
    assert now < verified.expires_at

    wider = narrow(
        _delegation(Grant.VIEW, Grant.INVESTIGATE, Grant.APPROVE),
        scopes=verified.scopes,
        expires_at=verified.expires_at,
        now=now,
    )
    assert wider.grants == {(REPO, Grant.VIEW), (REPO, Grant.INVESTIGATE)}
    view_only = _delegation(Grant.VIEW)
    narrowed = narrow(
        view_only, scopes=verified.scopes, expires_at=verified.expires_at, now=now
    )
    assert narrowed.grants == {(REPO, Grant.VIEW)}
    assert narrowed.expires_at <= view_only.expires_at

    # Plain names keep pytest's assertion rewriting from echoing the token.
    wrong_audience_refused = _rejected(
        Auth0TokenVerifier(domain=DOMAIN, audience="https://wrong.flakepilot.invalid"),
        token,
    )
    assert wrong_audience_refused
    head, claims, signature = token.split(".")
    flipped = ("B" if signature[0] == "A" else "A") + signature[1:]
    tampered_refused = _rejected(
        Auth0TokenVerifier(domain=DOMAIN, audience=AUDIENCE),
        f"{head}.{claims}.{flipped}",
    )
    assert tampered_refused
