"""Remove labelled containers the host ledger no longer holds open.

The worker does not call this. A client killed with SIGKILL can leave its
container running, because `--rm` is applied by the client. Discovery uses
`flakepilot.execution`. The deadline and the cancel flag come from the host
ledger written before start, not from the container label and not from a
worker lease. A label that names a later expiry does not extend the run. A
label that names an earlier expiry does not remove a run the ledger still
holds open. An unregistered label is removed. Two sweeps can run together:
removing an id that is already gone is not a failure of the other sweep. A
missing runtime, a runtime client that does not answer, or a ledger that is
missing, empty, or not mode `0600`, removes nothing.

The unit tests pass a fake CLI. They do not start a container and they do not
mark isolation checks passed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Protocol

from flakepilot.execution.application.host import ExecutionAuthority, LedgerError
from flakepilot.execution.application.plane import ReapResult
from flakepilot.execution.domain.plane import container_action, execution_id_ok

__all__ = ["CliFailed", "LabelledContainerReaper", "exec_cli"]

EXECUTION_LABEL = "flakepilot.execution"
EXPIRES_LABEL = "flakepilot.expires"
_CLIENT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


class Cli(Protocol):
    """Run one runtime argv and return stdout. No shell."""

    def __call__(self, argv: list[str]) -> str: ...


class CliFailed(Exception):
    """The runtime client failed or did not finish. Nothing was removed."""


def exec_cli(argv: list[str]) -> str:
    """Exec the runtime client with `PATH` only. The process environment is not copied.

    A client that does not finish is a failure. The caller removes nothing
    because of that failure.
    """
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            env=dict(_CLIENT_ENV),
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise CliFailed from None
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
        authority: ExecutionAuthority,
        runtime: str = "docker",
    ) -> None:
        if runtime not in ("docker", "podman"):
            raise ValueError("runtime must be docker or podman")
        self._cli = exec_cli if cli is None else cli
        self._now = now
        self._authority = authority
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
        try:
            chosen = _selected(text, self._authority, self._now())
        except LedgerError:
            return ReapResult(0, "runtime_reaper_unavailable")
        removed = 0
        for container_id in chosen:
            try:
                self._cli([self._runtime, "rm", "-f", container_id])
            except FileNotFoundError:
                return ReapResult(removed, "runtime_reaper_unavailable")
            except CliFailed:
                continue
            removed += 1
        return ReapResult(removed, "expired_containers_removed")


def _selected(text: str, authority: ExecutionAuthority, now: int) -> list[str]:
    chosen: list[str] = []
    for line in text.splitlines():
        container_id = _selected_id(line, authority, now)
        if container_id is not None:
            chosen.append(container_id)
    return chosen


def _selected_id(line: str, authority: ExecutionAuthority, now: int) -> str | None:
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    container_id, execution, _label_expires = (part.strip() for part in parts)
    if not _hex_id(container_id) or not execution_id_ok(execution):
        return None
    registered, cancelled, expires = authority.lookup(execution)
    if (
        container_action(
            registered=registered, cancelled=cancelled, expires=expires, now=now
        )
        != "remove"
    ):
        return None
    return container_id


def _hex_id(value: str) -> bool:
    return 12 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value
    )
