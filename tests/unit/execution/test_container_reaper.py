"""A fake docker CLI. This does not start a container or pass an isolation check."""

import subprocess

import pytest

from flakepilot.execution.domain.plane import wall_expired
from flakepilot.execution.infrastructure.reaper import (
    CliFailed,
    LabelledContainerReaper,
    exec_cli,
)

EXPIRED = "11111111-1111-1111-1111-111111111111"
ACTIVE = "22222222-2222-2222-2222-222222222222"
ORPHAN = "abc123abc123"
LIVE = "def456def456"


class FakeCli:
    def __init__(
        self,
        listing: str,
        *,
        fail_ps: Exception | None = None,
        fail_rm: Exception | None = None,
    ) -> None:
        self.listing = listing
        self.fail_ps = fail_ps
        self.fail_rm = fail_rm
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if argv[1] == "ps":
            if self.fail_ps is not None:
                raise self.fail_ps
            return self.listing
        if argv[1] == "rm" and self.fail_rm is not None:
            raise self.fail_rm
        return ""


def _listing() -> str:
    return "\n".join(
        [
            f"{ORPHAN}\t{EXPIRED}\t100",
            f"{LIVE}\t{ACTIVE}\t500",
            f"not-an-id\t{EXPIRED}\t100",
            f"fff456fff456\t{EXPIRED}\tsoon",
            "abc123abc124\tnot-a-uuid\t100",
        ]
    )


def test_a_killed_client_orphan_is_removed_and_a_live_run_is_not() -> None:
    """SIGKILL leaves the container running. Expiry, not status, decides."""
    cli = FakeCli(_listing())
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 1
    assert result.reason == "expired_containers_removed"
    removed = [call for call in cli.calls if call[1] == "rm"]
    assert removed == [["docker", "rm", "-f", ORPHAN]]
    listed = cli.calls[0]
    assert listed[0] == "docker"
    assert listed[1:3] == ["ps", "-a"]
    assert "label=flakepilot.execution" in listed
    assert LIVE not in [part for call in cli.calls for part in call[3:]]


def test_wall_expiry_ignores_a_worker_lease() -> None:
    """The deadline is the label. A lease is not an argument and cannot expire a run."""
    assert wall_expired(expires=500, now=200) is False
    assert wall_expired(expires=200, now=200) is True
    assert wall_expired(expires=100, now=200) is True


class _SharedCli:
    """Two reapers see one listing. The second sweep finds nothing left to remove."""

    def __init__(self) -> None:
        self.rows = [f"{ORPHAN}\t{EXPIRED}\t100", f"{LIVE}\t{ACTIVE}\t500"]
        self.removals: list[str] = []

    def __call__(self, argv: list[str]) -> str:
        if argv[1] == "ps":
            return "\n".join(self.rows)
        container_id = argv[-1]
        self.removals.append(container_id)
        self.rows = [
            row for row in self.rows if not row.startswith(container_id + "\t")
        ]
        return ""


def test_a_second_sweep_does_not_remove_the_container_again() -> None:
    cli = _SharedCli()
    first = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    second = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert first.reclaimed == 1
    assert second.reclaimed == 0
    assert second.reason == "expired_containers_removed"
    assert cli.removals == [ORPHAN]
    assert cli.rows == [f"{LIVE}\t{ACTIVE}\t500"]


def test_an_expiry_equal_to_the_clock_is_removed() -> None:
    cli = FakeCli(f"{ORPHAN}\t{EXPIRED}\t200")
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 1
    assert ["docker", "rm", "-f", ORPHAN] in cli.calls


def test_nothing_expired_still_means_the_runtime_was_inspected() -> None:
    cli = FakeCli(f"{LIVE}\t{ACTIVE}\t500")
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 0
    assert result.reason == "expired_containers_removed"
    assert [call[1] for call in cli.calls] == ["ps"]


def test_a_missing_runtime_removes_nothing() -> None:
    cli = FakeCli("", fail_ps=FileNotFoundError())
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 0
    assert result.reason == "runtime_reaper_unavailable"
    assert [call[1] for call in cli.calls] == ["ps"]


def test_a_failed_listing_does_not_remove() -> None:
    cli = FakeCli(_listing(), fail_ps=CliFailed())
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 0
    assert result.reason == "runtime_reaper_unavailable"


def test_a_failed_removal_is_not_counted() -> None:
    cli = FakeCli(f"{ORPHAN}\t{EXPIRED}\t100", fail_rm=CliFailed())
    result = LabelledContainerReaper(cli, now=lambda: 200).reclaim()
    assert result.reclaimed == 0
    assert result.reason == "expired_containers_removed"
    assert ["docker", "rm", "-f", ORPHAN] in cli.calls


def test_podman_is_the_only_other_runtime() -> None:
    cli = FakeCli("")
    LabelledContainerReaper(cli, now=lambda: 1, runtime="podman").reclaim()
    assert cli.calls[0][0] == "podman"
    with pytest.raises(ValueError):
        LabelledContainerReaper(cli, now=lambda: 1, runtime="bash")


def test_the_real_cli_does_not_inherit_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, timeout
        seen["argv"] = list(argv)
        seen["env"] = dict(env)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(
        "flakepilot.execution.infrastructure.reaper.subprocess.run", fake_run
    )
    monkeypatch.setenv("FLAKEPILOT_DATABASE_URL", "postgresql://secret")
    assert exec_cli(["docker", "ps"]) == ""
    assert seen["env"] == {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    assert "secret" not in str(seen["argv"])


def test_a_failing_real_cli_raises_without_returning_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, env, timeout
        return subprocess.CompletedProcess(argv, 1, b"no", b"")

    monkeypatch.setattr(
        "flakepilot.execution.infrastructure.reaper.subprocess.run", fake_run
    )
    with pytest.raises(CliFailed):
        exec_cli(["docker", "ps"])
