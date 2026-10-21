import asyncio
import json

import pytest
from support import FAKE_DSN, FakeEngineFactory, RawResponse, send_raw

from flakepilot.integrations.application.ports import (
    IdentityProviderError,
    VerifiedIdentity,
)
from flakepilot.interface.composition_root import (
    GitHubApp,
    Secret,
    Settings,
    create_app,
)

CLIENT_SECRET = "client-secret-SENTINEL-12"
SETTINGS = Settings(
    Secret(FAKE_DSN),
    public_origin="https://fp.example",
    github_app=GitHubApp("Iv1.test", Secret(CLIENT_SECRET)),
)


class FailingGitHub:
    def __init__(self, error: IdentityProviderError) -> None:
        self.error = error

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://github.test/authorize?state={state}"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            IdentityProviderError("http_502", unavailable=True),
            503,
            "auth_provider_unavailable",
        ),
        (
            IdentityProviderError("unavailable", unavailable=True),
            503,
            "auth_provider_unavailable",
        ),
        (IdentityProviderError("http_400"), 401, "unauthenticated"),
        (IdentityProviderError("code_rejected"), 401, "unauthenticated"),
        (IdentityProviderError("malformed_user"), 401, "unauthenticated"),
    ],
)
def test_callback_separates_provider_outages_from_rejections(
    error: IdentityProviderError,
    status: int,
    code: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(SETTINGS, FakeEngineFactory(), lambda _: FailingGitHub(error))

    async def scenario() -> RawResponse:
        async with app.router.lifespan_context(app):
            _, headers, _ = await send_raw(app, "GET", "/auth/github/login")
            bound = next(v for k, v in headers if k == "set-cookie").split(";")[0]
            state = bound.split("=", 1)[1].split(".")[0]
            callback = f"/auth/github/callback?code=c&state={state}"
            return await send_raw(app, "GET", callback, [("cookie", bound)])

    got, headers, body = asyncio.run(scenario())
    assert (got, json.loads(body)["error"]["code"]) == (status, code)
    cookies = [v for k, v in headers if k == "set-cookie"]
    assert any(
        c.startswith("__Host-flakepilot_sign_in=") and "Max-Age=0" in c for c in cookies
    )
    assert not any("flakepilot_session" in c for c in cookies)
    err = capsys.readouterr().err
    assert error.reason in err and CLIENT_SECRET not in err
