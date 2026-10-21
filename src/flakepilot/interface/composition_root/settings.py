"""Process settings, read once from environment variables (ADR-010). Empty means unset."""

import base64
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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

        return cls(
            Secret(database_url),
            log_level=log_level,
            host=host,
            port=int(port),
            public_origin=public_origin,
            github_app=github_app,
            secure_cookies=secure_cookies,
            secret_key=Secret(secret_key) if secret_key else None,
        )


def _origin(value: str, secure_cookies: bool) -> str:
    """Normalise to scheme://host[:port]; nothing else is allowed."""
    name = "FLAKEPILOT_PUBLIC_ORIGIN"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise SettingsError(f"{name} is not a valid origin") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SettingsError(f"{name} must be an http(s) origin")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise SettingsError(f"{name} must be only scheme, host and port")
    if parts.scheme == "http" and secure_cookies:
        raise SettingsError(
            f"{name} must use https unless FLAKEPILOT_ALLOW_INSECURE_COOKIES=true"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}"


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
