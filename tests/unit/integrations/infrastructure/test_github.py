import asyncio
import http.client
import io
import json
import urllib.parse
import urllib.request

import pytest

from flakepilot.integrations.application.ports import (
    IdentityProviderError,
    VerifiedIdentity,
)
from flakepilot.integrations.infrastructure.github import (
    AUTHORIZE_URL,
    MAX_RESPONSE_BYTES,
    TOKEN_URL,
    USER_URL,
    GitHubIdentityProvider,
    _read,
)

CLIENT_SECRET = "client-secret-SENTINEL-31"
GITHUB_TOKEN = "gho_SENTINEL-token-77"
Reply = tuple[int, object] | Exception


class FakeGitHub:
    """A fake HTTP transport: no network."""

    def __init__(
        self,
        token: Reply = (200, {"access_token": GITHUB_TOKEN, "token_type": "bearer"}),
        user: Reply = (200, {"id": 42, "login": "octo-cat"}),
    ) -> None:
        self.replies = {TOKEN_URL: token, USER_URL: user}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        self.requests.append(request)
        reply = self.replies[request.full_url]
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


def provider(fake: FakeGitHub) -> GitHubIdentityProvider:
    return GitHubIdentityProvider(
        client_id="Iv1.client",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://fp.example/auth/github/callback",
        transport=fake,
    )


def identify(fake: FakeGitHub) -> VerifiedIdentity:
    return asyncio.run(
        provider(fake).identify(code="the-code", code_verifier="verifier")
    )


def test_authorization_url_requests_pkce_s256() -> None:
    url = provider(FakeGitHub()).authorization_url(state="st", code_challenge="ch")
    base, _, query = url.partition("?")
    assert base == AUTHORIZE_URL
    assert urllib.parse.parse_qs(query) == {
        "client_id": ["Iv1.client"],
        "redirect_uri": ["https://fp.example/auth/github/callback"],
        "state": ["st"],
        "code_challenge": ["ch"],
        "code_challenge_method": ["S256"],
    }


def test_identity_comes_from_the_github_api_with_the_exchanged_token() -> None:
    fake = FakeGitHub()
    assert identify(fake) == VerifiedIdentity("https://github.com", "42", "octo-cat")
    token_request, user_request = fake.requests
    assert token_request.get_method() == "POST"
    assert isinstance(token_request.data, bytes)
    form = urllib.parse.parse_qs(token_request.data.decode())
    assert form["code"] == ["the-code"] and form["code_verifier"] == ["verifier"]
    assert user_request.get_header("Authorization") == f"Bearer {GITHUB_TOKEN}"
    assert all(r.full_url.startswith("https://") for r in fake.requests)


@pytest.mark.parametrize(
    ("fake", "reason", "unavailable"),
    [
        (FakeGitHub(token=(500, b"oops")), "http_500", True),
        (FakeGitHub(token=(503, b"")), "http_503", True),
        (FakeGitHub(token=(200, b"not json")), "malformed_response", True),
        (FakeGitHub(token=(200, b"[1, 2]")), "malformed_response", True),
        (FakeGitHub(token=TimeoutError("timed out")), "unavailable", True),
        (FakeGitHub(token=ConnectionRefusedError()), "unavailable", True),
        (FakeGitHub(token=http.client.BadStatusLine("x")), "unavailable", True),
        (FakeGitHub(user=http.client.IncompleteRead(b"")), "unavailable", True),
        (FakeGitHub(token=http.client.RemoteDisconnected("closed")), "unavailable", True),
        (FakeGitHub(token=(400, b"{}")), "http_400", False),
        (
            FakeGitHub(token=(200, {"error": "bad_verification_code"})),
            "code_rejected",
            False,
        ),
        (FakeGitHub(user=(401, b"{}")), "http_401", False),
        (FakeGitHub(user=(200, {"id": "42", "login": "x"})), "malformed_user", False),
        (FakeGitHub(user=(200, {"id": True, "login": "x"})), "malformed_user", False),
        (
            FakeGitHub(user=(200, {"id": 7, "login": "not a login"})),
            "malformed_user",
            False,
        ),
    ],
)
def test_failures_raise_reason_codes_without_secrets(
    fake: FakeGitHub, reason: str, unavailable: bool
) -> None:
    with pytest.raises(IdentityProviderError) as excinfo:
        identify(fake)
    assert excinfo.value.reason == reason
    assert excinfo.value.unavailable is unavailable
    assert CLIENT_SECRET not in repr(excinfo.value)
    assert GITHUB_TOKEN not in repr(excinfo.value)


def test_oversized_responses_are_rejected() -> None:
    limit = b"x" * MAX_RESPONSE_BYTES
    assert _read(io.BytesIO(limit)) == limit
    with pytest.raises(IdentityProviderError, match="response_too_large") as excinfo:
        _read(io.BytesIO(limit + b"x"))
    assert excinfo.value.unavailable
