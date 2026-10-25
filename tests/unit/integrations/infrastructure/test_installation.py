"""Installation tokens are contents:read, short-lived, and absent from errors."""

import asyncio
import base64
import json
import urllib.request
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from flakepilot.integrations.application.ports import CodeHostError
from flakepilot.integrations.infrastructure.code_host import GitHubCodeHost
from flakepilot.integrations.infrastructure.installation import InstallationMinter
from flakepilot.interface.composition_root.reproduction import _host
from flakepilot.interface.composition_root.settings import Secret, Settings

SHA = "ab" * 20
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
MINTED = "ghs_" + ("a" * 20)
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


class _Script:
    def __init__(self, steps: list[tuple[int, bytes]]) -> None:
        self._steps = list(steps)
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self, request: urllib.request.Request, limit: int
    ) -> tuple[int, bytes]:
        del limit
        self.requests.append(request)
        return self._steps.pop(0)


def _minter(script: _Script) -> InstallationMinter:
    return InstallationMinter(
        app_id="12345",
        private_key_pem=PEM,
        transport=script,
        clock=lambda: NOW,
    )


def _token_body() -> bytes:
    return json.dumps({"expires_at": "2026-10-21T16:00:00Z", "token": MINTED}).encode()


def test_a_token_is_contents_read_for_one_repo_and_signed() -> None:
    script = _Script([(200, b'{"id": 42}'), (200, _token_body())])
    host_calls: list[urllib.request.Request] = []

    def host_transport(
        request: urllib.request.Request, limit: int
    ) -> tuple[int, bytes]:
        del limit
        host_calls.append(request)
        return 200, f'{{"sha":"{SHA}"}}'.encode()

    host = GitHubCodeHost("static-bearer-value", host_transport, tokens=_minter(script))
    confirmed = asyncio.run(
        host.confirm_commit(owner="acme", name="web", claimed_sha=SHA)
    )
    assert confirmed == SHA
    lookup, created = script.requests
    assert lookup.full_url == "https://api.github.com/repos/acme/web/installation"
    assert lookup.get_method() == "GET"
    assert (
        created.full_url == "https://api.github.com/app/installations/42/access_tokens"
    )
    assert isinstance(created.data, bytes)
    posted = json.loads(created.data)
    assert posted == {"permissions": {"contents": "read"}, "repositories": ["web"]}
    jwt = str(lookup.get_header("Authorization"))
    assert jwt.startswith("Bearer ")
    _assert_signed(jwt.removeprefix("Bearer "))
    assert MINTED not in jwt
    sent = str(host_calls[0].get_header("Authorization"))
    assert sent == f"Bearer {MINTED}"
    assert "static-bearer-value" not in sent
    again = _Script([(200, b'{"id": 42}'), (200, _token_body())])
    minter = _minter(again)
    asyncio.run(minter.token(owner="acme", name="web"))
    asyncio.run(minter.token(owner="acme", name="web"))
    assert len(again.requests) == 2


def test_failures_do_not_echo_the_token_or_follow_a_redirect() -> None:
    secret = "ghs_secretvaluefromthehost"
    script = _Script([(302, secret.encode())])
    with pytest.raises(CodeHostError) as redirected:
        asyncio.run(_minter(script).token(owner="acme", name="web"))
    assert redirected.value.reason == "rejected_redirect"
    assert secret not in str(redirected.value)
    broken = _Script([(200, b'{"id": 42}'), (500, secret.encode())])
    with pytest.raises(CodeHostError) as failed:
        asyncio.run(_minter(broken).token(owner="acme", name="web"))
    assert failed.value.reason == "source_unverified"
    assert failed.value.unavailable
    assert secret not in str(failed.value)
    silent = _Script([(200, b"{}")])
    with pytest.raises(CodeHostError) as bad_name:
        asyncio.run(_minter(silent).token(owner="../evil", name="web"))
    assert bad_name.value.reason == "invalid_revision"
    assert silent.requests == []


def test_execution_ignores_a_static_contents_token() -> None:
    settings = Settings(
        Secret("postgresql://usr:pw@127.0.0.1:5432/db"),
        github_contents_token=Secret("ghs_" + ("b" * 20)),
    )
    host = _host(settings)
    assert host._token is None
    assert host._tokens is None
    with pytest.raises(CodeHostError) as excinfo:
        asyncio.run(host.confirm_commit(owner="acme", name="web", claimed_sha=SHA))
    assert excinfo.value.reason == "source_unverified"
    assert "ghs_" not in str(excinfo.value)


def _assert_signed(token: str) -> None:
    header, payload, signature = token.split(".")
    signing = f"{header}.{payload}".encode()
    pad = "=" * (-len(signature) % 4)
    raw = _b64decode(signature + pad)
    public = _KEY.public_key()
    assert isinstance(public, RSAPublicKey)
    public.verify(raw, signing, padding.PKCS1v15(), hashes.SHA256())
    body = json.loads(_b64decode(payload + "=" * (-len(payload) % 4)))
    assert body["iss"] == "12345"
    assert body["exp"] - body["iat"] == 600


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value)


def test_the_backfill_scope_asks_for_actions_read_only() -> None:
    script = _Script([(200, b'{"id": 42}'), (200, _token_body())])
    minter = InstallationMinter(
        app_id="12345",
        private_key_pem=PEM,
        transport=script,
        clock=lambda: NOW,
        scope="actions",
    )
    assert asyncio.run(minter.token(owner="acme", name="web")) == MINTED
    created = script.requests[1]
    assert isinstance(created.data, bytes)
    assert json.loads(created.data) == {
        "permissions": {"actions": "read"},
        "repositories": ["web"],
    }


def test_an_unknown_scope_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="invalid_scope"):
        InstallationMinter(app_id="12345", private_key_pem=PEM, scope="contents_write")


def test_github_answers_a_created_token_with_201() -> None:
    script = _Script([(200, b'{"id": 42}'), (201, _token_body())])
    assert asyncio.run(_minter(script).token(owner="acme", name="web")) == MINTED


def test_current_length_installation_tokens_are_accepted() -> None:
    long_token = "ghs_" + "A1b2.c-d_" * 43  # 391 characters, the live shape
    body = json.dumps({"expires_at": "2026-10-21T16:00:00Z", "token": long_token})
    script = _Script([(200, b'{"id": 42}'), (201, body.encode())])
    minter = _minter(script)
    assert asyncio.run(minter.token(owner="acme", name="web")) == long_token
    host_calls: list[urllib.request.Request] = []

    def host_transport(
        request: urllib.request.Request, limit: int
    ) -> tuple[int, bytes]:
        del limit
        host_calls.append(request)
        return 200, f'{{"sha":"{SHA}"}}'.encode()

    host = GitHubCodeHost(None, host_transport, tokens=minter)
    asyncio.run(host.confirm_commit(owner="acme", name="web", claimed_sha=SHA))
    assert host_calls[0].get_header("Authorization") == f"Bearer {long_token}"
