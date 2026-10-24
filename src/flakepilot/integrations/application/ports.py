"""Vendor-neutral port contracts owned by INT (ADR-003). Adapters live in infrastructure."""

import enum
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class VerifiedIdentity:
    """A subject verified by the identity provider. Authentication only: never authorization."""

    issuer: str
    subject: str
    login: str


class IdentityProviderError(Exception):
    """Sign-in could not be verified. `reason` is a short, non-sensitive code for logs.

    `unavailable` means the provider could not answer properly (network, timeout, 5xx,
    unreadable response); otherwise it rejected the sign-in (bad code, invalid identity).
    """

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class IdentityProvider(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    async def identify(self, *, code: str, code_verifier: str) -> VerifiedIdentity: ...


@dataclass(frozen=True)
class SecretScope:
    """Instance (no IDs), org, or repo (needs its org) scope of a secret."""

    org_id: str | None = None
    repo_id: str | None = None

    def __post_init__(self) -> None:
        if self.repo_id is not None and self.org_id is None:
            raise ValueError("a repo-scoped secret needs its org")


@dataclass(frozen=True)
class ArtifactRef:
    """A stored object: its key, SHA-256 (hex) and size. Keys are content-addressed."""

    key: str
    sha256: str
    size: int


class ArtifactStoreError(Exception):
    """`kind`: `not_found`, `integrity` (content does not match its hash) or
    `invalid_request`. Never carries content or paths."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ArtifactStore(Protocol):
    """Immutable blob storage (SYSTEM-ARCHITECTURE §4). Objects are never overwritten;
    the hash is checked on every read; keys are namespaced by owner, org and repo."""

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef: ...

    async def get(self, ref: ArtifactRef) -> bytes: ...


@dataclass(frozen=True)
class ParsedTestCase:
    """One test case from a report. `attempts` are outcomes in order: `passed`,
    `failed`, `errored` or `skipped`."""

    classname: str
    name: str
    attempts: tuple[str, ...]


class ReportRejected(Exception):
    """The report is refused. `reason` is a short machine-readable code; the report's
    content is never included."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReportParser(Protocol):
    """CI/report port (SYSTEM-ARCHITECTURE §4): deterministic, bounded parsing of
    untrusted bytes. Same bytes, same result."""

    def parse(self, data: bytes) -> list[ParsedTestCase]: ...


class SecretStore(Protocol):
    """Write-only for domain and application layers: no operation returns a secret value
    (ADR-003 decision 3). Values are resolved only inside INT adapters."""

    async def put(self, scope: SecretScope, name: str, value: str) -> str: ...

    async def delete(self, scope: SecretScope, name: str) -> None: ...


def _code(value: str, label: str) -> None:
    if _CODE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a short code")


@dataclass(frozen=True)
class AiCapabilities:
    """What an assist adapter can do. `available` is false when no model is wired."""

    available: bool
    provider: str

    def __post_init__(self) -> None:
        _code(self.provider, "provider")


@dataclass(frozen=True)
class AiDataPart:
    """One untrusted input. The adapter must not treat it as instructions."""

    name: str
    text: str

    def __post_init__(self) -> None:
        _code(self.name, "data part name")


@dataclass(frozen=True)
class AiRequest:
    """`instructions` are trusted. `data_parts` are untrusted evidence text."""

    instructions: str
    data_parts: tuple[AiDataPart, ...]
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True)
class AiResponse:
    """`available` carries `output` and no reason. Otherwise `output` is absent and
    `reason` is a short code. Model text never rides in `reason`."""

    available: bool
    output: str | None
    reason: str | None
    provider: str
    model: str | None

    def __post_init__(self) -> None:
        _code(self.provider, "provider")
        if self.model is not None:
            _code(self.model, "model")
        if self.available:
            if not isinstance(self.output, str) or self.reason is not None:
                raise ValueError("an available response has output and no reason")
        elif self.output is not None or self.reason is None:
            raise ValueError("an unavailable response has a reason and no output")
        elif _CODE.fullmatch(self.reason) is None:
            raise ValueError("reason must be a short code")


class AiAssistPort(Protocol):
    """Assist port (ADR-009). No tools and no side effects. Adapters do not authorize."""

    def describe(self) -> AiCapabilities: ...

    async def submit(self, request: AiRequest) -> AiResponse: ...


_MAX_EXCERPT_BYTES = 8 * 1024
# A source URL is http(s) only and at most 2048 bytes: it cannot be reached,
# cited or revisited otherwise.
_MAX_URL = 2_048


@dataclass(frozen=True)
class ResearchQuery:
    """One bounded research question. Sourced text is always untrusted data."""

    query: str
    max_sources: int = 5

    def __post_init__(self) -> None:
        length = len(self.query.encode())
        if not 1 <= length <= 512:
            raise ValueError("a research question is 1-512 bytes")
        if not 1 <= self.max_sources <= 10:
            raise ValueError("max_sources is 1-10")


@dataclass(frozen=True)
class ResearchSource:
    """One retrieved source with its provenance. The excerpt is untrusted content
    (`name: text` framing and the citation checker treat it as data, never as
    instructions); nothing here may carry budget or authority."""

    url: str
    title: str
    retrieved_at: datetime
    excerpt: str

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")) or len(self.url) > _MAX_URL:
            raise ValueError("a source URL is an http(s) URL of at most 2048 bytes")
        if len(self.title.encode()) > 200:
            raise ValueError("a source title is at most 200 bytes")
        if len(self.excerpt.encode()) > _MAX_EXCERPT_BYTES:
            raise ValueError("a source excerpt is at most 8192 bytes")


class ResearchProviderError(Exception):
    """Research could not be performed. `reason` is short; provider text is never
    echoed. `unavailable` means contact failed (network, timeout, 5xx, unreadable
    response); otherwise the provider refused the query."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class ResearchProvider(Protocol):
    """Bounded technical research (ADR-017 decision 4). Implementations must:
    honor the query budget (`max_sources`), deduplicate by URL, fail closed on
    every transport or shape failure with a short code, and never treat search
    results as anything but data. Deterministic fixtures serve local truth;
    live providers need owner-approved credentials."""

    def describe(self) -> AiCapabilities: ...

    async def search(self, query: ResearchQuery) -> tuple[ResearchSource, ...]: ...


@dataclass(frozen=True)
class SpeechRequest:
    """A text brief to speak. The text must already be clean (no secrets, no raw
    logs): the caller summarizes before this port is touched."""

    text: str
    voice: str = "default"

    def __post_init__(self) -> None:
        length = len(self.text.encode())
        if not 1 <= length <= 4_096:
            raise ValueError("a speech text is 1-4096 bytes")
        _code(self.voice, "voice")


@dataclass(frozen=True)
class SpeechAudio:
    """Generated audio: capped bytes and a fixed media type. `model` records
    provenance as a short code, per the AiResponse contract."""

    audio: bytes
    media_type: str
    provider: str
    model: str | None

    def __post_init__(self) -> None:
        if not self.audio or len(self.audio) > 2 * 1024 * 1024:
            raise ValueError("speech audio is 1 byte to 2 MiB")
        if not 1 <= len(self.media_type) <= 100:
            raise ValueError("a media type is 1-100 bytes")
        _code(self.provider, "provider")
        if self.model is not None:
            _code(self.model, "model")


class SpeechProviderError(Exception):
    """Text could not be spoken. Same reason rules as ResearchProviderError."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class SpeechProvider(Protocol):
    """Opt-in text-to-speech (ADR-017; goal 14). Absent by default: text-only
    operation is the fallback and needs no port at all. Live speech providers
    need owner-approved credentials; the text must never carry secrets."""

    def describe(self) -> AiCapabilities: ...

    async def speak(self, request: SpeechRequest) -> SpeechAudio: ...


_SHA = re.compile(r"[0-9a-f]{40}")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


def commit_sha_ok(value: str) -> bool:
    """40 lowercase hex digits. Any other shape is not a pinned commit."""
    return _SHA.fullmatch(value) is not None


def github_owner_ok(value: str) -> bool:
    return _OWNER.fullmatch(value) is not None


def github_name_ok(value: str) -> bool:
    return (
        _REPO_NAME.fullmatch(value) is not None
        and ".." not in value
        and not value.startswith(".")
        and "/" not in value
    )


_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def github_login_ok(value: str) -> bool:
    """A plausible GitHub account handle. Whether it belongs to anyone is a code-host
    question, not a shape question; callers must still refuse unusable shapes."""
    return _LOGIN.fullmatch(value) is not None


class CodeHostRole(enum.Enum):
    """A code-host account's repository permission, as the provider reports it
    (`none`, legacy base roles `read`/`write`/`admin`; the provider maps custom
    roles like `maintain` and `triage` onto these). Final names only; a role the
    provider reports that maps to none of these is not evidence."""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"

    @classmethod
    def from_report(cls, value: object) -> "CodeHostRole | None":
        """The role a provider response names, or None when the name is out of
        the known set: an unusable answer, never a permission."""
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                return None
        return None


class CodeHostError(Exception):
    """The code host did not confirm the commit. `reason` is a short code.

    `unavailable` means this process could not get a trustworthy answer. The
    claimed SHA is still unverified either way.
    """

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        self.reason = reason if _CODE.fullmatch(reason) else "invalid_error"
        self.unavailable = unavailable
        super().__init__(self.reason)


class CodeHost(Protocol):
    """Read-only commit pin and source fetch. Tokens never appear in the result."""

    async def confirm_commit(
        self, *, owner: str, name: str, claimed_sha: str
    ) -> str: ...

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes: ...


@dataclass(frozen=True)
class SandboxFile:
    """One capped output. `kind` is `result` or `screenshot`. Bytes are untrusted."""

    kind: str
    data: bytes

    def __post_init__(self) -> None:
        if self.kind not in ("result", "screenshot"):
            raise ValueError("sandbox file kind is result or screenshot")
        if not isinstance(self.data, bytes):
            raise TypeError("sandbox file data is bytes")


@dataclass(frozen=True)
class SandboxReport:
    """What the runtime client observed. Not an execution outcome.

    `status` is `finished`, `infra`, `timed_out`, `cancelled`, or `closed`.
    `reason` is a short code. `files` are untrusted bytes, possibly empty.
    """

    status: str
    reason: str
    files: tuple[SandboxFile, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("finished", "infra", "timed_out", "cancelled", "closed"):
            raise ValueError("sandbox status is not a known runtime status")
        if _CODE.fullmatch(self.reason) is None:
            raise ValueError("sandbox reason must be a short code")


class CancelFlag(Protocol):
    def requested(self) -> bool: ...


class Sandbox(Protocol):
    """Provision, run the approved command, collect, teardown. Idempotent teardown.

    The adapter chooses the command. `selector` and `sha` are data. A missing
    runtime returns a report; it does not run the command on the host.
    """

    async def run(
        self,
        *,
        execution_id: str,
        selector: str,
        sha: str,
        source_zip: bytes,
        wall_seconds: int,
        memory_mb: int,
        pids: int,
        cancel: CancelFlag | None = None,
    ) -> SandboxReport: ...
