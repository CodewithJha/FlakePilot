"""GitHub commit pin and zipball fetch (ADR-009). No GitHub SDK.

The URL is always `https://api.github.com/repos/{owner}/{name}/...`. Owner, name,
and SHA are checked before they are interpolated. Redirects are refused. The
bearer token is a header, never part of the URL, the exception, or the result.

With no token the transport is not called and the answer is `source_unverified`.
A `tokens` source is asked per repository and wins over any static bearer.
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
    CodeHost,
    CodeHostError,
    CodeHostRole,
    commit_sha_ok,
    github_login_ok,
    github_name_ok,
    github_owner_ok,
)

API = "https://api.github.com"
TIMEOUT = 10.0
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024

Transport = Callable[[urllib.request.Request, int], tuple[int, bytes]]
_BEARER = re.compile(r"[A-Za-z0-9._-]{1,1024}")  # installation tokens are ~390


class TokenSource(Protocol):
    """Mints a bearer for one repository. The value must not be logged."""

    async def token(self, *, owner: str, name: str) -> str: ...


__all__ = ["CodeHost", "CodeHostError", "GitHubCodeHost"]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


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


def _read(stream: Any, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if not isinstance(body, bytes) or len(body) > limit:
        raise CodeHostError("response_too_large", unavailable=True)
    return body


class GitHubCodeHost:
    def __init__(
        self,
        token: str | None,
        transport: Transport = urllib_transport,
        *,
        tokens: TokenSource | None = None,
    ) -> None:
        self._token = token
        self._transport = transport
        self._tokens = tokens

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        url = self._url(owner, name, f"commits/{claimed_sha}")
        status, body = await self._call(url, MAX_JSON_BYTES, owner, name)
        if status in (301, 302, 303, 307, 308):
            raise CodeHostError("rejected_redirect")
        if status == 404:
            raise CodeHostError("unverified_revision")
        if status != 200:
            raise CodeHostError("source_unverified", unavailable=status >= 500)
        try:
            payload = json.loads(body)
        except ValueError:
            raise CodeHostError("source_unverified", unavailable=True) from None
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if sha != claimed_sha:
            raise CodeHostError("unverified_revision")
        return claimed_sha

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        url = self._url(owner, name, f"zipball/{sha}")
        status, body = await self._call(url, MAX_ARCHIVE_BYTES, owner, name)
        if status in (301, 302, 303, 307, 308):
            raise CodeHostError("rejected_redirect")
        if status != 200 or not body:
            raise CodeHostError("source_unverified", unavailable=status >= 500)
        return body

    async def repository_permission(
        self, *, owner: str, name: str, login: str
    ) -> CodeHostRole:
        """The account's repository permission, as the code host reports it for this
        canonical repository (GET .../collaborators/{login}/permission: the provider
        maps custom roles like maintain and triage onto the base roles).

        The answer is the role, or a `CodeHostError`: 404 is ambiguous (the account
        may not be a collaborator, or the installation may not see the repository),
        and every other refused answer is a lookup this deployment could not make.
        Neither becomes a permission. No token is able to call: same as source.
        """
        if not (
            github_owner_ok(owner) and github_name_ok(name) and github_login_ok(login)
        ):
            raise CodeHostError("invalid_revision")
        url = (
            f"{API}/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}/collaborators/"
            f"{urllib.parse.quote(login, safe='')}/permission"
        )
        if not url.startswith(f"{API}/"):
            raise CodeHostError("invalid_revision")
        status, body = await self._call(url, MAX_JSON_BYTES, owner, name)
        if status in (301, 302, 303, 307, 308):
            raise CodeHostError("rejected_redirect")
        if status == 404:
            # Ambiguous on purpose: not one verifiable permission level.
            raise CodeHostError("access_unverified", unavailable=False)
        if status != 200:
            # Refused or rate-limited lookups, bad credentials, provider trouble:
            # none is a permission fact about the account.
            raise CodeHostError("access_unverified", unavailable=True)
        try:
            payload = json.loads(body)
        except ValueError:
            raise CodeHostError("source_unverified", unavailable=True) from None
        role = payload.get("permission") if isinstance(payload, dict) else None
        resolved = CodeHostRole.from_report(role)
        if resolved is None:
            raise CodeHostError("source_unverified", unavailable=True)
        return resolved

    def _url(self, owner: str, name: str, tail: str) -> str:
        if not (
            github_owner_ok(owner)
            and github_name_ok(name)
            and commit_sha_ok(tail[-40:])
        ):
            raise CodeHostError("invalid_revision")
        owner_q = urllib.parse.quote(owner, safe="")
        name_q = urllib.parse.quote(name, safe="")
        return f"{API}/repos/{owner_q}/{name_q}/{tail}"

    async def _bearer(self, owner: str, name: str) -> str:
        minted = (
            await self._tokens.token(owner=owner, name=name)
            if self._tokens is not None
            else self._token
        )
        if not minted or _BEARER.fullmatch(minted) is None:
            raise CodeHostError("source_unverified", unavailable=True)
        return minted

    async def _call(
        self, url: str, limit: int, owner: str, name: str
    ) -> tuple[int, bytes]:
        bearer = await self._bearer(owner, name)
        if not url.startswith(f"{API}/"):
            raise CodeHostError("invalid_revision")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "FlakePilot",
            },
        )
        try:
            return await asyncio.to_thread(self._transport, request, limit)
        except CodeHostError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise CodeHostError("source_unverified", unavailable=True) from None
