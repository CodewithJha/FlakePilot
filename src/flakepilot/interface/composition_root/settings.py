"""Process settings, read once from environment variables (ADR-010). Empty means unset."""

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
class Settings:
    database_url: Secret
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000

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

        return cls(Secret(database_url), log_level=log_level, host=host, port=int(port))


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
