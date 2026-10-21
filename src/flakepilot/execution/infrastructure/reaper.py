"""Remove expired labelled containers. The worker does not call this.

A client killed with SIGKILL can leave its container running, because `--rm`
is applied by the client. This adapter lists containers that carry
`flakepilot.execution` and `flakepilot.expires`, and removes only those whose
expiry is at or before the caller's clock. An active run has a later expiry
and is left alone. A missing runtime removes nothing.

The unit tests pass a fake CLI. They do not start a container and they do not
mark isolation checks passed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Protocol

from flakepilot.execution.application.plane import ReapResult

__all__ = ["CliFailed", "LabelledContainerReaper", "exec_cli"]

EXECUTION_LABEL = "flakepilot.execution"
EXPIRES_LABEL = "flakepilot.expires"
_CLIENT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


class Cli(Protocol):
    """Run one runtime argv and return stdout. No shell."""

    def __call__(self, argv: list[str]) -> str: ...


class CliFailed(Exception):
    """The runtime client ran and returned a failure. Nothing was removed."""


def exec_cli(argv: list[str]) -> str:
    """Exec the runtime client with `PATH` only. The process environment is not copied."""
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        env=dict(_CLIENT_ENV),
        timeout=10,
    )
    if completed.returncode != 0:
        raise CliFailed
    return completed.stdout.decode("utf-8", errors="replace")


class LabelledContainerReaper:
    """Sweep one runtime. `now` is unix seconds from the caller."""

    def __init__(
        self,
        cli: Cli | None = None,
        *,
        now: Callable[[], int],
        runtime: str = "docker",
    ) -> None:
        if runtime not in ("docker", "podman"):
            raise ValueError("runtime must be docker or podman")
        self._cli = exec_cli if cli is None else cli
        self._now = now
        self._runtime = runtime

    def reclaim(self) -> ReapResult:
        listing = [
            self._runtime,
            "ps",
            "-a",
            "--filter",
            f"label={EXECUTION_LABEL}",
            "--format",
            (
                '{{.ID}}\t{{.Label "'
                + EXECUTION_LABEL
                + '"}}\t{{.Label "'
                + EXPIRES_LABEL
                + '"}}'
            ),
        ]
        try:
            text = self._cli(listing)
        except (FileNotFoundError, CliFailed):
            return ReapResult(0, "runtime_reaper_unavailable")
        removed = 0
        moment = self._now()
        for container_id in _expired(text, moment):
            try:
                self._cli([self._runtime, "rm", "-f", container_id])
            except FileNotFoundError:
                return ReapResult(removed, "runtime_reaper_unavailable")
            except CliFailed:
                continue
            removed += 1
        return ReapResult(removed, "expired_containers_removed")


def _expired(text: str, now: int) -> list[str]:
    chosen: list[str] = []
    for line in text.splitlines():
        container_id = _expired_id(line, now)
        if container_id is not None:
            chosen.append(container_id)
    return chosen


def _expired_id(line: str, now: int) -> str | None:
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    container_id, execution, expires = (part.strip() for part in parts)
    if not _hex_id(container_id) or not _execution_id(execution):
        return None
    if not expires.isascii() or not expires.isdigit():
        return None
    if int(expires) > now:
        return None
    return container_id


def _hex_id(value: str) -> bool:
    return 12 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _execution_id(value: str) -> bool:
    pieces = value.split("-")
    widths = (8, 4, 4, 4, 12)
    if len(pieces) != len(widths):
        return False
    return all(
        len(piece) == width
        and all(character in "0123456789abcdef" for character in piece)
        for piece, width in zip(pieces, widths, strict=True)
    )
