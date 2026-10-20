"""Process settings, read once from environment variables (ADR-010). Empty means unset."""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class SettingsError(ValueError):
    """Invalid configuration. Messages name the variable and never include its value."""


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "Settings":
        default = cls()

        log_level = (environ.get("FLAKEPILOT_LOG_LEVEL") or default.log_level).upper()
        if log_level not in LOG_LEVELS:
            raise SettingsError(
                f"FLAKEPILOT_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}"
            )

        host = environ.get("FLAKEPILOT_HOST") or default.host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise SettingsError("FLAKEPILOT_HOST must be an IP address") from None

        port = environ.get("FLAKEPILOT_PORT") or str(default.port)
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise SettingsError("FLAKEPILOT_PORT must be an integer from 1 to 65535")

        return cls(log_level=log_level, host=host, port=int(port))
