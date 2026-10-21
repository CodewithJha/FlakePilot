"""The GitHub pin never follows a redirect or trusts a caller-supplied SHA."""

import asyncio
import urllib.request

import pytest

from flakepilot.integrations.application.ports import CodeHostError
from flakepilot.integrations.infrastructure.code_host import GitHubCodeHost

SHA = "ab" * 20
OTHER = "cd" * 20


class _Transport:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.urls: list[str] = []
        self.headers: list[object] = []

    def __call__(
        self, request: urllib.request.Request, limit: int
    ) -> tuple[int, bytes]:
        self.urls.append(request.full_url)
        self.headers.append(request.header_items())
        return self.status, self.body


def test_no_token_does_not_call_the_network() -> None:
    transport = _Transport(200, b"{}")
    host = GitHubCodeHost(None, transport)
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(host.confirm_commit(owner="acme", name="web", claimed_sha=SHA))
    assert excinfo.value.reason == "source_unverified"
    assert excinfo.value.unavailable
    assert transport.urls == []


def test_confirm_requires_the_json_sha_and_keeps_the_token_out_of_the_url() -> None:
    transport = _Transport(200, f'{{"sha": "{SHA}"}}'.encode())
    host = GitHubCodeHost("secret-token", transport)
    confirmed = asyncio.run(
        host.confirm_commit(owner="acme", name="web", claimed_sha=SHA)
    )
    assert confirmed == SHA
    assert transport.urls == [f"https://api.github.com/repos/acme/web/commits/{SHA}"]
    assert "secret-token" not in transport.urls[0]
    assert "secret-token" in repr(transport.headers)


def test_a_host_sha_that_differs_is_rejected() -> None:
    transport = _Transport(200, f'{{"sha": "{OTHER}"}}'.encode())
    host = GitHubCodeHost("secret-token", transport)
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(host.confirm_commit(owner="acme", name="web", claimed_sha=SHA))
    assert excinfo.value.reason == "unverified_revision"
    assert "secret-token" not in str(excinfo.value)


def test_redirect_and_traversal_never_become_a_pin() -> None:
    transport = _Transport(302, b"")
    host = GitHubCodeHost("secret-token", transport)
    with pytest.raises(CodeHostError) as redirected:
        asyncio.run(host.confirm_commit(owner="acme", name="web", claimed_sha=SHA))
    assert redirected.value.reason == "rejected_redirect"
    with pytest.raises(CodeHostError) as traversed:
        asyncio.run(host.fetch_archive(owner="../evil", name="web", sha=SHA))
    assert traversed.value.reason == "invalid_revision"
    assert len(transport.urls) == 1
