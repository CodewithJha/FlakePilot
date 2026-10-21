"""Process settings, read once from environment variables (ADR-010). Empty means unset."""

import base64
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from flakepilot.integrations.infrastructure.artifact_store import root_problem
from flakepilot.integrations.infrastructure.sandbox import image_ok

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class SettingsError(ValueError):
    """Invalid configuration. Messages name the variable and never include its value."""


@dataclass(frozen=True)
class Secret:
    """A value kept out of repr, str and logs. Call `reveal()` only at the point of use."""

    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


@dataclass(frozen=True)
class GitHubApp:
    """GitHub App credentials for sign-in (ADR-009). From the environment at bootstrap."""

    client_id: str
    client_secret: Secret


@dataclass(frozen=True)
class Settings:
    database_url: Secret
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    # Sign-in is enabled only when both are set; auth endpoints answer 503 otherwise.
    public_origin: str | None = None
    github_app: GitHubApp | None = None
    secure_cookies: bool = True
    # 32-byte key for the encrypted secret store (ADR-009), base64; not wired yet.
    secret_key: Secret | None = None
    # Artifact storage root (ADR-013). Ingestion answers 503 while it is unset.
    artifact_dir: Path | None = None
    # Leased worker (`... worker`). Empty means these defaults. The API process ignores them.
    worker_poll_seconds: int = 2
    worker_batch_size: int = 10
    worker_lease_seconds: int = 30
    # closed: never start a container. evaluation: explicit opt-in, not production.
    execution_mode: str = "closed"
    execution_runtime: str = "docker"
    execution_image: str | None = None
    execution_wall_seconds: int = 30
    execution_memory_mb: int = 512
    execution_pids: int = 64
    # Optional bearer for GitHub contents reads. Empty means revisions stay unverified.
    github_contents_token: Secret | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "Settings":
        database_url = environ.get("FLAKEPILOT_DATABASE_URL")
        if not database_url:
            raise SettingsError("FLAKEPILOT_DATABASE_URL is required")
        _validate_database_url(database_url)

        log_level = (environ.get("FLAKEPILOT_LOG_LEVEL") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise SettingsError(
                f"FLAKEPILOT_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}"
            )

        host = environ.get("FLAKEPILOT_HOST") or "127.0.0.1"
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise SettingsError("FLAKEPILOT_HOST must be an IP address") from None

        port = environ.get("FLAKEPILOT_PORT") or "8000"
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise SettingsError("FLAKEPILOT_PORT must be an integer from 1 to 65535")

        insecure = (environ.get("FLAKEPILOT_ALLOW_INSECURE_COOKIES") or "false").lower()
        if insecure not in ("true", "false"):
            raise SettingsError(
                "FLAKEPILOT_ALLOW_INSECURE_COOKIES must be true or false"
            )
        secure_cookies = insecure == "false"

        public_origin = environ.get("FLAKEPILOT_PUBLIC_ORIGIN") or None
        if public_origin is not None:
            public_origin = _origin(public_origin, secure_cookies)

        client_id = environ.get("FLAKEPILOT_GITHUB_CLIENT_ID") or None
        client_secret = environ.get("FLAKEPILOT_GITHUB_CLIENT_SECRET") or None
        github_app = None
        if client_id or client_secret:
            for name, value in (
                ("FLAKEPILOT_GITHUB_CLIENT_ID", client_id),
                ("FLAKEPILOT_GITHUB_CLIENT_SECRET", client_secret),
                ("FLAKEPILOT_PUBLIC_ORIGIN", public_origin),
            ):
                if not value:
                    raise SettingsError(f"{name} is required for GitHub sign-in")
            assert client_id and client_secret
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", client_id):
                raise SettingsError(
                    "FLAKEPILOT_GITHUB_CLIENT_ID is not a valid client ID"
                )
            github_app = GitHubApp(client_id, Secret(client_secret))

        secret_key = environ.get("FLAKEPILOT_SECRET_KEY") or None
        if secret_key is not None and len(_decode_key(secret_key)) != 32:
            raise SettingsError(
                "FLAKEPILOT_SECRET_KEY must be 32 bytes, base64-encoded"
            )

        artifact_dir = environ.get("FLAKEPILOT_ARTIFACT_DIR") or None
        return cls(
            Secret(database_url),
            log_level=log_level,
            host=host,
            port=int(port),
            public_origin=public_origin,
            github_app=github_app,
            secure_cookies=secure_cookies,
            secret_key=Secret(secret_key) if secret_key else None,
            artifact_dir=_artifact_dir(artifact_dir) if artifact_dir else None,
            worker_poll_seconds=_bounded(
                environ, "FLAKEPILOT_WORKER_POLL_SECONDS", 2, 1, 300
            ),
            worker_batch_size=_bounded(
                environ, "FLAKEPILOT_WORKER_BATCH_SIZE", 10, 1, 100
            ),
            worker_lease_seconds=_bounded(
                environ, "FLAKEPILOT_WORKER_LEASE_SECONDS", 30, 5, 3600
            ),
            execution_mode=_execution_mode(environ),
            execution_runtime=_execution_runtime(environ),
            execution_image=_execution_image(environ),
            execution_wall_seconds=_bounded(
                environ, "FLAKEPILOT_EXECUTION_WALL_SECONDS", 30, 5, 120
            ),
            execution_memory_mb=_bounded(
                environ, "FLAKEPILOT_EXECUTION_MEMORY_MB", 512, 64, 2048
            ),
            execution_pids=_bounded(environ, "FLAKEPILOT_EXECUTION_PIDS", 64, 8, 256),
            github_contents_token=_contents_token(environ),
        )


def _bounded(
    environ: Mapping[str, str], name: str, default: int, low: int, high: int
) -> int:
    raw = environ.get(name) or str(default)
    if not re.fullmatch(r"[0-9]{1,4}", raw) or not low <= int(raw) <= high:
        raise SettingsError(f"{name} must be an integer from {low} to {high}")
    return int(raw)


def _execution_mode(environ: Mapping[str, str]) -> str:
    mode = environ.get("FLAKEPILOT_EXECUTION_MODE") or "closed"
    if mode not in ("closed", "evaluation"):
        raise SettingsError("FLAKEPILOT_EXECUTION_MODE must be closed or evaluation")
    return mode


def _execution_runtime(environ: Mapping[str, str]) -> str:
    runtime = environ.get("FLAKEPILOT_EXECUTION_RUNTIME") or "docker"
    if runtime not in ("docker", "podman"):
        raise SettingsError("FLAKEPILOT_EXECUTION_RUNTIME must be docker or podman")
    return runtime


def _execution_image(environ: Mapping[str, str]) -> str | None:
    image = environ.get("FLAKEPILOT_EXECUTION_IMAGE") or None
    mode = environ.get("FLAKEPILOT_EXECUTION_MODE") or "closed"
    if image is not None and not image_ok(image):
        raise SettingsError(
            "FLAKEPILOT_EXECUTION_IMAGE must be a digest-pinned reference"
        )
    if mode == "evaluation" and image is None:
        raise SettingsError("FLAKEPILOT_EXECUTION_IMAGE is required for evaluation")
    return image


def _contents_token(environ: Mapping[str, str]) -> Secret | None:
    token = environ.get("FLAKEPILOT_GITHUB_CONTENTS_TOKEN") or None
    if token is None:
        return None
    if len(token) > 500 or any(character.isspace() for character in token):
        raise SettingsError("FLAKEPILOT_GITHUB_CONTENTS_TOKEN is not a token")
    return Secret(token)


def _artifact_dir(value: str) -> Path:
    path = Path(value)
    problem = root_problem(path)
    if problem is not None:
        raise SettingsError(f"FLAKEPILOT_ARTIFACT_DIR {problem}")
    return path


def _origin(value: str, secure_cookies: bool) -> str:
    """Normalise to scheme://host[:port] as browsers send it in `Origin` (lowercase
    host, default port omitted); nothing else is allowed."""
    name = "FLAKEPILOT_PUBLIC_ORIGIN"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise SettingsError(f"{name} is not a valid origin") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SettingsError(f"{name} must be an http(s) origin")
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
    ):
        raise SettingsError(f"{name} must be only scheme, host and port")
    if parts.scheme == "http" and secure_cookies:
        raise SettingsError(
            f"{name} must use https unless FLAKEPILOT_ALLOW_INSECURE_COOKIES=true"
        )
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default else ""
    return f"{parts.scheme}://{host}{suffix}"


def _decode_key(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise SettingsError("FLAKEPILOT_SECRET_KEY is not valid base64") from None


def _validate_database_url(url: str) -> None:
    """Structural checks only; errors never include any part of the URL."""
    name = "FLAKEPILOT_DATABASE_URL"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise SettingsError(f"{name} is not a valid URL") from None
    if parts.scheme not in ("postgresql", "postgres"):
        raise SettingsError(f"{name} must use the postgresql:// scheme")
    if not parts.hostname:
        raise SettingsError(f"{name} must include a host")
    if not parts.path.strip("/") or "/" in parts.path.strip("/"):
        raise SettingsError(f"{name} must include a database name")
