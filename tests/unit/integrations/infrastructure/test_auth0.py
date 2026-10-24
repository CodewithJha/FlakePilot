"""Auth0 agent-token verifier: every refusal is the same bare refusal."""

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256

from flakepilot.integrations.infrastructure.auth0 import (
    JWKS_REFRESH_SECONDS,
    Auth0TokenVerifier,
    TokenRejected,
)

DOMAIN = "flakepilot-test.us.auth0.com"
ISSUER = f"https://{DOMAIN}/"
AUDIENCE = "https://api.flakepilot.example"
NOW = 1_800_000_000.0
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WEAK = rsa.generate_private_key(public_exponent=65537, key_size=1024)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def segment(value: Any) -> str:
    return b64(json.dumps(value).encode())


def jwk(key: rsa.RSAPrivateKey, kid: str, **extra: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
        "e": b64(numbers.e.to_bytes(3)),
        **extra,
    }


def claims(**overrides: Any) -> dict[str, Any]:
    base = {
        "iss": ISSUER,
        "sub": "agentclient123@clients",
        "aud": AUDIENCE,
        "iat": NOW - 10,
        "exp": NOW + 600,
        "scope": "repo.view repo.investigate",
        "gty": "client-credentials",
    }
    return {k: v for k, v in {**base, **overrides}.items() if v is not None}


def token(
    body: dict[str, Any] | None = None,
    *,
    key: rsa.RSAPrivateKey = KEY,
    header: dict[str, Any] | None = None,
) -> str:
    signing_input = (
        f"{segment(header or {'alg': 'RS256', 'typ': 'JWT', 'kid': 'k1'})}."
        f"{segment(claims() if body is None else body)}"
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), SHA256())
    return f"{signing_input}.{b64(signature)}"


class Tenant:
    """The JWKS endpoint, counting contacts; `fail` makes it unreachable."""

    def __init__(self, keys: list[dict[str, str]]) -> None:
        self.keys = keys
        self.urls: list[str] = []
        self.fail = False

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail:
            raise OSError("unreachable")
        return json.dumps({"keys": self.keys}).encode()


def verifier(
    tenant: Tenant, clock: Callable[[], float] = lambda: NOW
) -> Auth0TokenVerifier:
    return Auth0TokenVerifier(
        domain=DOMAIN, audience=AUDIENCE, fetch=tenant, clock=clock
    )


def rejected(subject: Auth0TokenVerifier, value: str) -> TokenRejected:
    with pytest.raises(TokenRejected) as error:
        asyncio.run(subject.verify(value))
    assert str(error.value) == ""
    assert error.value.__cause__ is None and error.value.__suppress_context__
    return error.value


def test_a_valid_agent_token_yields_subject_scopes_and_expiry() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    verified = asyncio.run(verifier(tenant).verify(token()))
    assert verified.subject == "agentclient123@clients"
    assert verified.scopes == {"repo.view", "repo.investigate"}
    assert verified.expires_at.timestamp() == NOW + 600
    assert tenant.urls == [f"https://{DOMAIN}/.well-known/jwks.json"]


def test_an_audience_list_containing_the_api_is_accepted() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    body = claims(aud=[f"https://{DOMAIN}/userinfo", AUDIENCE])
    assert asyncio.run(verifier(tenant).verify(token(body))).scopes


@pytest.mark.parametrize(
    "body",
    [
        claims(exp=NOW),
        claims(exp=NOW - 1),
        claims(exp=None),
        claims(exp="9999999999"),
        claims(exp=True),
        claims(nbf=NOW + 1),
        claims(iss=f"https://{DOMAIN}"),
        claims(iss="https://evil.example/"),
        claims(aud="https://other-api.example"),
        claims(aud=[]),
        claims(aud=None),
        claims(scope=None),
        claims(scope=""),
        claims(scope="   "),
        claims(scope=["repo.view"]),
        claims(sub=None),
        claims(sub=""),
    ],
)
def test_claims_outside_the_contract_are_refused(body: dict[str, Any]) -> None:
    rejected(verifier(Tenant([jwk(KEY, "k1")])), token(body))


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": "k1"},
        {"alg": "RS512", "kid": "k1"},
        {"alg": "PS256", "kid": "k1"},
        {"kid": "k1"},
        {"alg": "RS256"},
        {"alg": "RS256", "kid": ""},
        {"alg": "RS256", "kid": "k1", "crit": ["exp"]},
    ],
)
def test_only_rs256_with_a_kid_is_considered(header: dict[str, Any]) -> None:
    rejected(verifier(Tenant([jwk(KEY, "k1")])), token(header=header))


def test_alg_none_and_hs256_key_confusion_are_refused() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    unsigned = f"{segment({'alg': 'none', 'kid': 'k1'})}.{segment(claims())}."
    rejected(verifier(tenant), unsigned)
    public_pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = f"{segment({'alg': 'HS256', 'kid': 'k1'})}.{segment(claims())}"
    mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    rejected(verifier(tenant), f"{signing_input}.{b64(mac)}")


def test_a_bad_signature_or_tampered_claims_are_refused() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    rejected(verifier(tenant), token(key=OTHER))
    head, _, signature = token().split(".")
    widened = segment(claims(scope="repo.view repo.approve"))
    rejected(verifier(tenant), f"{head}.{widened}.{signature}")


def test_weak_keys_and_non_signing_keys_in_the_jwks_are_ignored() -> None:
    rejected(verifier(Tenant([jwk(WEAK, "k1")])), token(key=WEAK))
    rejected(verifier(Tenant([jwk(KEY, "k1", use="enc")])), token())
    rejected(verifier(Tenant([jwk(KEY, "k1", alg="RS512")])), token())


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a.b",
        "a.b.c.d",
        "!!.??.**",
        token() + "x" * 9000,
        f"{segment([1])}.{segment(claims())}.AA",
        # A duplicate member could smuggle a second `alg` past a naive parser.
        f"{b64(b'{"alg":"RS256","kid":"k1","alg":"none"}')}.{segment(claims())}.AA",
    ],
)
def test_malformed_tokens_are_refused(value: str) -> None:
    rejected(verifier(Tenant([jwk(KEY, "k1")])), value)


def test_unknown_kid_refetches_at_most_once_per_refresh_interval() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    now = [NOW]
    subject = verifier(tenant, clock=lambda: now[0])
    assert asyncio.run(subject.verify(token()))
    stranger = token(header={"alg": "RS256", "kid": "forged"})
    rejected(subject, stranger)
    rejected(subject, stranger)
    assert len(tenant.urls) == 1  # cached keys answer; forged kids cost nothing
    tenant.keys.append(jwk(OTHER, "k2"))
    rotated = token(key=OTHER, header={"alg": "RS256", "kid": "k2"})
    rejected(subject, rotated)
    now[0] += JWKS_REFRESH_SECONDS
    body = claims(exp=now[0] + 600)
    rotated = token(body, key=OTHER, header={"alg": "RS256", "kid": "k2"})
    assert asyncio.run(subject.verify(rotated)).subject
    assert len(tenant.urls) == 2


def test_an_unreachable_or_garbled_jwks_refuses() -> None:
    tenant = Tenant([jwk(KEY, "k1")])
    tenant.fail = True
    rejected(verifier(tenant), token())

    def garbled(url: str) -> bytes:
        return b"<html>not json</html>"

    rejected(
        Auth0TokenVerifier(
            domain=DOMAIN, audience=AUDIENCE, fetch=garbled, clock=lambda: NOW
        ),
        token(),
    )
