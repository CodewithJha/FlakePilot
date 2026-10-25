"""GitHub Actions CI history for the operator backfill (goal 12). No GitHub SDK.

Reads, for one repository, as FlakePilot's GitHub App installation with an
actions:read token: completed workflow runs, a run's artifacts, and one
artifact's zip. URLs are always `https://api.github.com/repos/{owner}/{name}/
actions/...`, built from checked values. The API answers an artifact download
with a redirect to signed storage; exactly that one redirect is followed, only
to an https GitHub storage host, and the bearer token is never sent there.
Every body is size-capped; failures are `CodeHostError` short codes, never
response content.

`extract_reports` opens an artifact zip as hostile input: entries are only
read into memory (never written to disk), capped per entry and in total,
encrypted or unusually compressed entries are refused, and a name that could
traverse a path refuses the whole archive. JUnit parsing itself stays with the
hardened `JUnitParser`.
"""

import asyncio
import http.client
import io
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flakepilot.integrations.application.ports import (
    CodeHostError,
    ReportRejected,
    commit_sha_ok,
    github_name_ok,
    github_owner_ok,
)
from flakepilot.integrations.infrastructure.code_host import TokenSource
from flakepilot.integrations.infrastructure.junit import MAX_BYTES as MAX_REPORT_BYTES

__all__ = [
    "CodeHostError",
    "GitHubActionsHistory",
    "ReportRejected",
    "RunArtifact",
    "WorkflowRun",
    "extract_reports",
]

API = "https://api.github.com"
TIMEOUT = 20.0
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024  # the redirect answer to an artifact download
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_REPORT_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_RUNS = 100  # one page; the operator command asks for at most 50
STORAGE_HOST_SUFFIXES = (".blob.core.windows.net", ".actions.githubusercontent.com")
_WORKFLOW = re.compile(r"[0-9]{1,20}|[A-Za-z0-9._-]{1,100}\.ya?ml")
_BRANCH = re.compile(r"(?!.*\.\.)[A-Za-z0-9._/-]{1,200}")
_BEARER = re.compile(r"[A-Za-z0-9._-]{1,1024}")

# (request, response byte cap) -> (status, lower-cased headers, body)
Transport = Callable[
    [urllib.request.Request, int], tuple[int, Mapping[str, str], bytes]
]


@dataclass(frozen=True)
class WorkflowRun:
    """One completed run as the API reports it. `completed_at` is the run's
    `updated_at`, which GitHub stamps when a completed run finishes."""

    id: int
    attempt: int
    head_sha: str
    head_branch: str | None
    started_at: datetime
    completed_at: datetime
    url: str  # built here from owner, name, id and attempt; not taken from the API


@dataclass(frozen=True)
class RunArtifact:
    id: int
    name: str
    size: int
    expired: bool


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _read(stream: Any, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if not isinstance(body, bytes) or len(body) > limit:
        raise CodeHostError("response_too_large", unavailable=True)
    return body


def urllib_transport(
    request: urllib.request.Request, limit: int
) -> tuple[int, Mapping[str, str], bytes]:
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, headers, _read(response, limit)
    except urllib.error.HTTPError as error:
        with error:
            headers = {k.lower(): v for k, v in (error.headers or {}).items()}
            return error.code, headers, _read(error, limit)


def storage_url_ok(url: str) -> bool:
    """https, no credentials, a GitHub artifact storage host."""
    if len(url) > 4096:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and "@" not in parts.netloc
        and parts.port in (None, 443)
        and any(host.endswith(suffix) for suffix in STORAGE_HOST_SUFFIXES)
    )


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _positive(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class GitHubActionsHistory:
    def __init__(self, tokens: TokenSource, transport: Transport = urllib_transport):
        self._tokens = tokens
        self._transport = transport

    async def completed_runs(
        self,
        *,
        owner: str,
        name: str,
        limit: int,
        workflow: str | None = None,
        branch: str | None = None,
    ) -> tuple[WorkflowRun, ...]:
        """At most `limit` completed runs, newest first (one page). Runs the API
        reports in an unexpected shape are left out, never guessed."""
        if not 1 <= limit <= MAX_RUNS:
            raise CodeHostError("invalid_request")
        if workflow is not None and _WORKFLOW.fullmatch(workflow) is None:
            raise CodeHostError("invalid_request")
        if branch is not None and _BRANCH.fullmatch(branch) is None:
            raise CodeHostError("invalid_request")
        tail = (
            f"actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs"
            if workflow is not None
            else "actions/runs"
        )
        query = {
            "status": "completed",
            "per_page": str(limit),
            "exclude_pull_requests": "true",
        }
        if branch is not None:
            query["branch"] = branch
        payload = await self._json(owner, name, tail, query)
        found = payload.get("workflow_runs")
        if not isinstance(found, list):
            raise CodeHostError("malformed_response", unavailable=True)
        runs: list[WorkflowRun] = []
        for item in found[:limit]:
            run = self._run(owner, name, item)
            if run is not None:
                runs.append(run)
        return tuple(runs)

    async def artifacts(
        self, *, owner: str, name: str, run_id: int
    ) -> tuple[RunArtifact, ...]:
        if _positive(run_id) is None:
            raise CodeHostError("invalid_request")
        payload = await self._json(
            owner, name, f"actions/runs/{run_id}/artifacts", {"per_page": "100"}
        )
        found = payload.get("artifacts")
        if not isinstance(found, list):
            raise CodeHostError("malformed_response", unavailable=True)
        artifacts: list[RunArtifact] = []
        for item in found:
            if not isinstance(item, dict):
                continue
            artifact_id = _positive(item.get("id"))
            label, size = item.get("name"), item.get("size_in_bytes")
            if (
                artifact_id is None
                or not isinstance(label, str)
                or not 1 <= len(label) <= 256
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                continue
            artifacts.append(
                RunArtifact(artifact_id, label, size, item.get("expired") is True)
            )
        return tuple(artifacts)

    async def download(self, *, owner: str, name: str, artifact: RunArtifact) -> bytes:
        """The artifact's zip, at most `MAX_ARCHIVE_BYTES`."""
        if artifact.expired:
            raise CodeHostError("artifact_expired")
        if artifact.size > MAX_ARCHIVE_BYTES:
            raise CodeHostError("artifact_too_large")
        url = self._url(owner, name, f"actions/artifacts/{artifact.id}/zip", None)
        status, headers, body = await self._call(
            await self._request(owner, name, url), MAX_JSON_BYTES
        )
        if status == 200 and body:  # a host that serves the zip directly
            return body
        if status == 410:
            raise CodeHostError("artifact_expired")
        if status != 302:
            raise CodeHostError(f"http_{status}", unavailable=status >= 500)
        location = headers.get("location", "")
        if not storage_url_ok(location):
            raise CodeHostError("rejected_redirect")
        # Signed storage URL: no Authorization header, and no further redirect.
        storage = urllib.request.Request(location, headers={"User-Agent": "FlakePilot"})
        status, _, body = await self._call(storage, MAX_ARCHIVE_BYTES)
        if status != 200 or not body:
            raise CodeHostError(f"http_{status}", unavailable=status >= 500)
        return body

    def _run(self, owner: str, name: str, item: object) -> WorkflowRun | None:
        if not isinstance(item, dict) or item.get("status") != "completed":
            return None
        run_id, attempt = _positive(item.get("id")), _positive(item.get("run_attempt"))
        sha, branch = item.get("head_sha"), item.get("head_branch")
        started = _instant(item.get("run_started_at") or item.get("created_at"))
        completed = _instant(item.get("updated_at"))
        if (
            run_id is None
            or attempt is None
            or not isinstance(sha, str)
            or not commit_sha_ok(sha)
            or not (branch is None or isinstance(branch, str))
            or started is None
            or completed is None
            or completed < started
        ):
            return None
        url = (
            f"https://github.com/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}/actions/runs/{run_id}"
            f"/attempts/{attempt}"
        )
        return WorkflowRun(run_id, attempt, sha, branch, started, completed, url)

    def _url(
        self, owner: str, name: str, tail: str, query: Mapping[str, str] | None
    ) -> str:
        if not (github_owner_ok(owner) and github_name_ok(name)):
            raise CodeHostError("invalid_request")
        url = (
            f"{API}/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}/{tail}"
        )
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    async def _request(self, owner: str, name: str, url: str) -> urllib.request.Request:
        bearer = await self._tokens.token(owner=owner, name=name)
        if not bearer or _BEARER.fullmatch(bearer) is None:
            raise CodeHostError("source_unverified", unavailable=True)
        if not url.startswith(f"{API}/"):
            raise CodeHostError("invalid_request")
        return urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "FlakePilot",
            },
        )

    async def _call(
        self, request: urllib.request.Request, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        try:
            return await asyncio.to_thread(self._transport, request, limit)
        except CodeHostError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise CodeHostError("source_unverified", unavailable=True) from None

    async def _json(
        self, owner: str, name: str, tail: str, query: Mapping[str, str]
    ) -> dict[str, Any]:
        url = self._url(owner, name, tail, query)
        status, _, body = await self._call(
            await self._request(owner, name, url), MAX_LIST_BYTES
        )
        if status in (301, 302, 303, 307, 308):
            raise CodeHostError("rejected_redirect")
        if status != 200:
            raise CodeHostError(f"http_{status}", unavailable=status >= 500)
        try:
            data = json.loads(body)
        except ValueError:
            raise CodeHostError("malformed_response", unavailable=True) from None
        if not isinstance(data, dict):
            raise CodeHostError("malformed_response", unavailable=True)
        return data


_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f\\]")


def _safe_name(filename: str) -> bool:
    if not 1 <= len(filename) <= 512 or _UNSAFE_NAME.search(filename):
        return False
    if filename.startswith("/") or re.match(r"[A-Za-z]:", filename):
        return False
    return ".." not in filename.split("/")


def extract_reports(archive: bytes) -> tuple[bytes, ...]:
    """The `.xml` entries of an artifact zip, in entry-name order. Raises
    `ReportRejected` with a short code for anything unsafe or malformed."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ReportRejected("archive_too_large")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ReportRejected("too_many_entries")
            if not all(_safe_name(entry.filename) for entry in entries):
                raise ReportRejected("unsafe_entry_name")
            reports: list[bytes] = []
            total = 0
            for entry in sorted(entries, key=lambda e: e.filename):
                if entry.is_dir() or not entry.filename.lower().endswith(".xml"):
                    continue
                if entry.flag_bits & 0x1:
                    raise ReportRejected("encrypted_entry")
                if entry.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise ReportRejected("unsupported_compression")
                if entry.file_size > MAX_REPORT_BYTES:
                    raise ReportRejected("too_large")
                with bundle.open(entry) as stream:
                    data = stream.read(MAX_REPORT_BYTES + 1)
                if len(data) > MAX_REPORT_BYTES:
                    raise ReportRejected("too_large")
                total += len(data)
                if total > MAX_TOTAL_REPORT_BYTES:
                    raise ReportRejected("archive_too_large")
                reports.append(data)
    except ReportRejected:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        OSError,
        ValueError,
        NotImplementedError,
        RuntimeError,
    ):
        raise ReportRejected("malformed_archive") from None
    return tuple(reports)
