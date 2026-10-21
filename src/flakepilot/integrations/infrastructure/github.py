"""GitHub sign-in adapter (ADR-009, ADR-011 E): GitHub App user authorization with PKCE.

Outbound HTTPS uses the standard library in a worker thread: TLS verified with the default
context, redirects refused, explicit timeout, capped response size. The GitHub user access
token is used once to read the user's ID and login, then discarded; nothing needs it later.
Failures raise IdentityProviderError with a short reason code, never response content.
"""

import asyncio
import http.client
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from flakepilot.integrations.application.ports import (
    IdentityProviderError,
    VerifiedIdentity,
)

ISSUER = "https://github.com"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
TIMEOUT = 10.0  # seconds per HTTP call
MAX_RESPONSE_BYTES = 64 * 1024
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

Transport = Callable[[urllib.request.Request], tuple[int, bytes]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect becomes an HTTPError instead of being followed


class _Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def _read(stream: _Readable) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise IdentityProviderError("response_too_large", unavailable=True)
    return body


def urllib_transport(request: urllib.request.Request) -> tuple[int, bytes]:
    """Blocking HTTPS call; run it off the event loop."""
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, _read(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, _read(error)


class GitHubIdentityProvider:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: Transport = urllib_transport,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._transport = transport

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity:
        form = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": code_verifier,
            }
        ).encode()
        token = await self._json(
            urllib.request.Request(
                TOKEN_URL,
                data=form,
                method="POST",
                headers={"Accept": "application/json", "User-Agent": "FlakePilot"},
            )
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise IdentityProviderError("code_rejected")
        user = await self._json(
            urllib.request.Request(
                USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "FlakePilot",
                },
            )
        )
        user_id, login = user.get("id"), user.get("login")
        if not (
            isinstance(user_id, int)
            and not isinstance(user_id, bool)
            and user_id > 0
            and isinstance(login, str)
            and _LOGIN.fullmatch(login)
        ):
            raise IdentityProviderError("malformed_user")
        return VerifiedIdentity(issuer=ISSUER, subject=str(user_id), login=login)

    async def _json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            status, body = await asyncio.to_thread(self._transport, request)
        except IdentityProviderError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            # Network, TLS, timeout; or a broken HTTP exchange (BadStatusLine,
            # IncompleteRead, RemoteDisconnected, ...).
            raise IdentityProviderError("unavailable", unavailable=True) from None
        if status != 200:
            # 4xx: GitHub rejected the request (e.g. the code); 5xx: GitHub is failing.
            raise IdentityProviderError(f"http_{status}", unavailable=status >= 500)
        try:
            data = json.loads(body)
        except ValueError:
            raise IdentityProviderError(
                "malformed_response", unavailable=True
            ) from None
        if not isinstance(data, dict):
            raise IdentityProviderError("malformed_response", unavailable=True)
        return data
