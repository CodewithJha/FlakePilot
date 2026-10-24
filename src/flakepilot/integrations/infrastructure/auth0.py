"""Auth0 agent-token verification (sponsor goal 9): RS256 access tokens, one API.

Verifies an access token Auth0 issued to an AI agent (client-credentials or
on-behalf-of-user) for exactly one configured API audience. Signature keys come
only from the tenant's own `https://<domain>/.well-known/jwks.json`; a token can
never name its key's location (`jku`, `x5u`, `jwk` are ignored) or algorithm
(only RS256: `none`, HS* and every other `alg` are refused before any key is used).

A verified token is evidence about the agent software, never authority: callers
may only use it to narrow a delegation FlakePilot itself minted
(`identity.application.agent_tokens.narrow`).

Fail closed, no detail: every refusal (malformed, oversized, wrong alg, unknown
kid, bad signature, wrong iss/aud, expired, not yet valid, no scope, JWKS fetch
failure) raises a bare `TokenRejected` with no message and no chained cause.
Unconfigured issuer means no verifier exists at all (composition root).
"""

import asyncio
import base64
import binascii
import json
import re
import ssl
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicKey,
    RSAPublicNumbers,
)
from cryptography.hazmat.primitives.hashes import SHA256

MAX_TOKEN_BYTES = 8192
MAX_JWKS_BYTES = 64 * 1024
MIN_RSA_BITS = 2048
#: An unknown `kid` refetches the key set at most this often (key rotation
#: without letting forged kids turn every request into a tenant call).
JWKS_REFRESH_SECONDS = 300
TIMEOUT = 5.0
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")

Fetch = Callable[[str], bytes]


class TokenRejected(Exception):
    """The token is not honored. Deliberately carries no reason."""


@dataclass(frozen=True)
class AgentToken:
    subject: str
    scopes: frozenset[str]
    expires_at: datetime


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def https_fetch(url: str) -> bytes:
    """Blocking GET: TLS verified, redirects refused, bounded time and size."""
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    with opener.open(url, timeout=TIMEOUT) as response:
        body: bytes = response.read(MAX_JWKS_BYTES + 1)
    if len(body) > MAX_JWKS_BYTES:
        raise ValueError("oversized key set")
    return body


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate member")
    return result


def _bytes(segment: str) -> bytes:
    if not _SEGMENT.fullmatch(segment):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _object(segment: str) -> dict[str, Any]:
    value = json.loads(_bytes(segment), object_pairs_hook=_no_duplicates)
    if not isinstance(value, dict):
        raise TypeError("not an object")
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_jwks(body: bytes) -> dict[str, RSAPublicKey]:
    """RSA signing keys by kid. Entries that are not usable RS256 keys are skipped."""
    keys: dict[str, RSAPublicKey] = {}
    document = json.loads(body)
    entries = document.get("keys") if isinstance(document, dict) else None
    for jwk in entries if isinstance(entries, list) else []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
            continue
        kid, n, e = jwk.get("kid"), jwk.get("n"), jwk.get("e")
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256":
            continue
        if not (isinstance(kid, str) and isinstance(n, str) and isinstance(e, str)):
            continue
        try:
            key = RSAPublicNumbers(
                int.from_bytes(_bytes(e)), int.from_bytes(_bytes(n))
            ).public_key()
        except (ValueError, binascii.Error):
            continue
        if key.key_size >= MIN_RSA_BITS:
            keys[kid] = key
    return keys


class Auth0TokenVerifier:
    def __init__(
        self,
        *,
        domain: str,
        audience: str,
        fetch: Fetch = https_fetch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._issuer = f"https://{domain}/"
        self._jwks_url = f"https://{domain}/.well-known/jwks.json"
        self._audience = audience
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._fetched_at: float | None = None

    async def verify(self, token: str) -> AgentToken:
        try:
            return await self._verify(token)
        except Exception:  # noqa: BLE001 - every failure is the same refusal
            raise TokenRejected from None

    async def _key(self, kid: str) -> RSAPublicKey | None:
        now = self._clock()
        stale = (
            self._fetched_at is None or now - self._fetched_at >= JWKS_REFRESH_SECONDS
        )
        if kid not in self._keys and stale:
            self._fetched_at = now
            self._keys = parse_jwks(
                await asyncio.to_thread(self._fetch, self._jwks_url)
            )
        return self._keys.get(kid)

    async def _verify(self, token: str) -> AgentToken:
        if len(token) > MAX_TOKEN_BYTES:
            raise ValueError("oversized")
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = _object(encoded_header)
        if header.get("alg") != "RS256" or "crit" in header:
            raise ValueError("algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
            raise ValueError("kid")
        key = await self._key(kid)
        if key is None:
            raise ValueError("unknown kid")
        try:
            key.verify(
                _bytes(encoded_signature),
                f"{encoded_header}.{encoded_claims}".encode("ascii"),
                PKCS1v15(),
                SHA256(),
            )
        except InvalidSignature:
            raise ValueError("signature") from None
        return self._claims(_object(encoded_claims))

    def _claims(self, claims: dict[str, Any]) -> AgentToken:
        now = self._clock()
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        expires = _number(claims.get("exp"))
        not_before = _number(claims.get("nbf", 0))
        subject, scope = claims.get("sub"), claims.get("scope")
        if (
            claims.get("iss") != self._issuer
            or self._audience not in audiences
            or expires is None
            or now >= expires
            or not_before is None
            or now < not_before
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 255
            or not isinstance(scope, str)
            or not scope.split()
        ):
            raise ValueError("claims")
        return AgentToken(
            subject=subject,
            scopes=frozenset(scope.split()),
            expires_at=datetime.fromtimestamp(expires, UTC),
        )
