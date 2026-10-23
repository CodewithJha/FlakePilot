"""The GitHub pin never follows a redirect or trusts a caller-supplied SHA."""

import asyncio
import urllib.request

import pytest

from flakepilot.integrations.application.ports import CodeHostError, CodeHostRole
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


def test_repository_permission_maps_the_reported_role() -> None:
    transport = _Transport(
        200, b'{"permission": "write", "role_name": "maintain", "user": {"id": 7}}'
    )
    host = GitHubCodeHost("secret-token", transport)
    role = asyncio.run(
        host.repository_permission(owner="acme", name="web", login="member")
    )
    assert role is CodeHostRole.WRITE
    assert transport.urls == [
        "https://api.github.com/repos/acme/web/collaborators/member/permission"
    ]
    assert "secret-token" not in transport.urls[0]


def test_repository_permission_accepts_the_base_roles() -> None:
    for reported, expected in (
        ("admin", CodeHostRole.ADMIN),
        ("write", CodeHostRole.WRITE),
        ("read", CodeHostRole.READ),
        ("none", CodeHostRole.NONE),
    ):
        transport = _Transport(200, f'{{"permission": "{reported}"}}'.encode())
        host = GitHubCodeHost("secret-token", transport)
        role = asyncio.run(
            host.repository_permission(owner="acme", name="web", login="member")
        )
        assert role is expected, reported


def test_a_404_is_ambiguous_and_never_a_permission() -> None:
    """A 404 can mean "not a collaborator" or "the installation cannot see the
    repository"; neither is one verifiable permission level."""
    transport = _Transport(404, b'{"message": "Not Found"}')
    host = GitHubCodeHost("secret-token", transport)
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(host.repository_permission(owner="acme", name="web", login="ghost"))
    assert excinfo.value.reason == "access_unverified"
    assert not excinfo.value.unavailable


def test_refused_and_failed_lookups_are_unavailable() -> None:
    for status in (401, 403, 429, 500, 503):
        transport = _Transport(status, b"{}")
        host = GitHubCodeHost("secret-token", transport)
        with pytest.raises(CodeHostError) as excinfo:
            asyncio.run(
                host.repository_permission(owner="acme", name="web", login="member")
            )
        assert excinfo.value.reason == "access_unverified"
        assert excinfo.value.unavailable, status
        assert "secret-token" not in str(excinfo.value)


def test_a_malformed_permission_answer_is_not_a_role() -> None:
    for body in (b'{"permission": "flying"}', b"not-json", b'{"permission": 3}'):
        transport = _Transport(200, body)
        host = GitHubCodeHost("secret-token", transport)
        with pytest.raises(CodeHostError) as excinfo:
            asyncio.run(
                host.repository_permission(owner="acme", name="web", login="member")
            )
        assert excinfo.value.reason == "source_unverified"
        assert excinfo.value.unavailable


def test_no_token_never_asks_about_a_permission() -> None:
    transport = _Transport(200, b'{"permission": "admin"}')
    host = GitHubCodeHost(None, transport)
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(
            host.repository_permission(owner="acme", name="web", login="member")
        )
    assert excinfo.value.reason == "source_unverified"
    assert excinfo.value.unavailable
    assert transport.urls == []  # fail closed without contacting the host


def test_a_redirect_and_bad_shapes_never_become_a_permission() -> None:
    host = GitHubCodeHost("secret-token", _Transport(302, b""))
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(
            host.repository_permission(owner="acme", name="web", login="member")
        )
    assert excinfo.value.reason == "rejected_redirect"
    with pytest.raises(CodeHostError) as traversed:
        asyncio.run(
            host.repository_permission(owner="../evil", name="web", login="member")
        )
    assert traversed.value.reason == "invalid_revision"


def test_an_unusable_login_is_refused_without_a_call() -> None:
    transport = _Transport(200, b'{"permission": "admin"}')
    host = GitHubCodeHost("secret-token", transport)
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(
            host.repository_permission(owner="acme", name="web", login="../etc")
        )
    assert excinfo.value.reason == "invalid_revision"
    assert transport.urls == []
