"""Short-lived GitHub App installation tokens (ADR-009).

A token is contents:read for one repository, created only when a caller asks
for that owner and name. It is not logged. Missing or unusable credentials do
not call the network. This is not a write token and it is not a user bearer.
"""

import asyncio
import base64
import http.client
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from flakepilot.integrations.application.ports import (
    CodeHostError,
    github_name_ok,
    github_owner_ok,
)

API = "https://api.github.com"
TIMEOUT = 10.0
MAX_JSON_BYTES = 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9._-]{8,255}")
_APP_ID = re.compile(r"[1-9][0-9]{0,19}")

Transport = Callable[[urllib.request.Request, int], tuple[int, bytes]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def urllib_transport(request: urllib.request.Request, limit: int) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, _read(response, limit)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, _read(error, limit)


def private_key_ok(pem: str) -> bool:
    """True when `pem` is an unencrypted RSA private key. Never raises the key."""
    if len(pem) > 16_000:
        return False
    try:
        _load(pem)
    except (ValueError, TypeError):
        return False
    return True


def app_id_ok(value: str) -> bool:
    return _APP_ID.fullmatch(value) is not None


class InstallationMinter:
    """Mints one contents:read token. The value stays in this object until it expires."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not app_id_ok(app_id):
            raise ValueError("invalid_key")
        self._app_id = app_id
        try:
            self._key = _load(private_key_pem)
        except (ValueError, TypeError):
            raise ValueError("invalid_key") from None
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[tuple[str, str], tuple[str, datetime]] = {}

    async def token(self, *, owner: str, name: str) -> str:
        if not (github_owner_ok(owner) and github_name_ok(name)):
            raise CodeHostError("invalid_revision")
        now = self._clock()
        if now.tzinfo is None:
            raise CodeHostError("source_unverified", unavailable=True)
        cached = self._cache.get((owner, name))
        if cached is not None and cached[1] - timedelta(seconds=60) > now:
            return cached[0]
        installation_id = await self._installation_id(owner, name)
        minted, expiry = await self._access_token(installation_id, name, now)
        self._cache[(owner, name)] = (minted, expiry)
        return minted

    async def _installation_id(self, owner: str, name: str) -> int:
        status, body = await self._exchange(
            self._request("GET", f"/repos/{_quote(owner)}/{_quote(name)}/installation")
        )
        if status != 200:
            raise CodeHostError("source_unverified", unavailable=status >= 500)
        found = _object(body).get("id")
        if isinstance(found, bool) or not isinstance(found, int) or found <= 0:
            raise CodeHostError("source_unverified", unavailable=True)
        return found

    async def _access_token(
        self, installation_id: int, name: str, now: datetime
    ) -> tuple[str, datetime]:
        body = json.dumps(
            {"permissions": {"contents": "read"}, "repositories": [name]},
            separators=(",", ":"),
        ).encode()
        status, payload = await self._exchange(
            self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                body,
            )
        )
        if status != 200:
            raise CodeHostError("source_unverified", unavailable=status >= 500)
        data = _object(payload)
        minted = data.get("token")
        expiry = _expiry(data.get("expires_at"), now)
        if (
            not isinstance(minted, str)
            or _TOKEN.fullmatch(minted) is None
            or expiry is None
        ):
            raise CodeHostError("source_unverified", unavailable=True)
        return minted, expiry

    def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> urllib.request.Request:
        return urllib.request.Request(
            f"{API}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "FlakePilot",
            },
        )

    def _jwt(self) -> str:
        stamp = int(self._clock().timestamp())
        header = _b64(b'{"alg":"RS256","typ":"JWT"}')
        payload = _b64(
            json.dumps(
                {"exp": stamp + 540, "iat": stamp - 60, "iss": self._app_id},
                separators=(",", ":"),
            ).encode()
        )
        signing = f"{header}.{payload}".encode()
        signature = self._key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        return signing.decode() + "." + _b64(signature)

    async def _exchange(self, request: urllib.request.Request) -> tuple[int, bytes]:
        try:
            status, body = await asyncio.to_thread(
                self._transport, request, MAX_JSON_BYTES
            )
        except CodeHostError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise CodeHostError("source_unverified", unavailable=True) from None
        if status in (301, 302, 303, 307, 308):
            raise CodeHostError("rejected_redirect")
        return status, body


def _load(pem: str) -> RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(_pem_text(pem).encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("invalid_key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("invalid_key")
    return key


def _pem_text(value: str) -> str:
    text = value.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if text.startswith("-----BEGIN "):
        return text
    try:
        decoded = base64.b64decode("".join(text.split()), validate=True)
    except ValueError:
        return text
    if decoded.startswith(b"-----BEGIN "):
        return decoded.decode("ascii")
    return text


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _object(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        raise CodeHostError("source_unverified", unavailable=True) from None
    if not isinstance(data, dict):
        raise CodeHostError("source_unverified", unavailable=True)
    return data


def _expiry(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed <= now:
        return None
    return parsed


def _read(stream: _Readable, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if not isinstance(body, bytes) or len(body) > limit:
        raise CodeHostError("response_too_large", unavailable=True)
    return body
