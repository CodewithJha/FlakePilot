"""The adapter never runs the approved command on the host."""

import asyncio
import io
import os
import stat
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from flakepilot.execution.domain.reproduction import APPROVED_COMMAND as DOMAIN_COMMAND
from flakepilot.integrations.application.ports import CancelFlag
from flakepilot.integrations.infrastructure.sandbox import (
    APPROVED_COMMAND,
    PNG_MAGIC,
    ClosedSandbox,
    ContainerSandbox,
    RuntimeMissing,
    reap_workdirs,
)

IMAGE = "ghcr.io/example/reproduce@sha256:" + ("ab" * 32)
SHA = "cd" * 20
EXECUTION = "11111111-1111-1111-1111-111111111111"
SELECTOR = "tests.login.TestLogin.test_ok"


class _Flag:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def requested(self) -> bool:
        return self.value


def _zip(
    entries: list[tuple[str, bytes]] | None = None, *, link: str | None = None
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries or [("src/test_ok.py", b"print('no')")]:
            archive.writestr(name, data)
        if link is not None:
            info = zipfile.ZipInfo(link)
            info.external_attr = stat.S_IFLNK << 16
            archive.writestr(info, "x")
    return buffer.getvalue()


def _output(argv: list[str]) -> Path:
    for argument in argv:
        if argument.endswith(":/workspace/out:rw"):
            return Path(argument.split(":", 1)[0])
    raise AssertionError("missing output mount")


def test_closed_mode_starts_nothing() -> None:
    result = asyncio.run(
        ClosedSandbox().run(
            execution_id=EXECUTION,
            selector=SELECTOR,
            sha=SHA,
            source_zip=_zip(),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
    )
    assert result.status == "closed"
    assert result.reason == "execution_closed"
    assert result.files == ()
    assert APPROVED_COMMAND == DOMAIN_COMMAND


def test_container_argv_is_fixed_and_the_host_env_is_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def runner(
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        cancel: CancelFlag | None,
    ) -> int:
        del timeout, cancel
        seen["argv"] = list(argv)
        seen["env"] = dict(env)
        out = _output(argv)
        out.joinpath("result.json").write_bytes(
            b'{"status":"passed","selector":"tests.login.TestLogin.test_ok"}'
        )
        out.joinpath("shot.png").write_bytes(PNG_MAGIC + b"img")
        return 0

    monkeypatch.setenv("FLAKEPILOT_DATABASE_URL", "postgresql://secret")
    sandbox = ContainerSandbox(
        runtime="docker",
        image=IMAGE,
        runner=runner,
        work_root=tmp_path,
        clock=lambda: 1_000.0,
    )
    result = asyncio.run(
        sandbox.run(
            execution_id=EXECUTION,
            selector=SELECTOR,
            sha=SHA,
            source_zip=_zip(),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
    )
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "docker"
    assert argv[-1] == APPROVED_COMMAND
    joined = " ".join(str(part) for part in argv)
    for flag in (
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--user=65534:65534",
        "--pids-limit=16",
        "--memory=128m",
    ):
        assert flag in argv
    assert "flakepilot.execution=" + EXECUTION in argv
    assert "flakepilot.expires=1005" in argv
    assert "--privileged" not in argv
    assert "docker.sock" not in joined
    assert "--network=host" not in argv
    assert seen["env"] == {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    assert "secret" not in joined
    assert result.status == "finished"
    assert [item.kind for item in result.files] == ["result", "screenshot"]
    assert list(tmp_path.iterdir()) == []


def test_a_label_is_not_written_for_an_unsafe_execution_id(tmp_path: Path) -> None:
    seen: list[str] = []

    async def runner(
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        cancel: CancelFlag | None,
    ) -> int:
        del env, timeout, cancel
        seen.extend(argv)
        return 0

    sandbox = ContainerSandbox(
        runtime="docker", image=IMAGE, runner=runner, work_root=tmp_path
    )
    asyncio.run(
        sandbox.run(
            execution_id="$(touch)",
            selector=SELECTOR,
            sha=SHA,
            source_zip=_zip(),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
    )
    assert not any(part.startswith("flakepilot.") for part in seen)


def test_unsafe_archives_and_a_missing_runtime_do_not_run(tmp_path: Path) -> None:
    calls = 0

    async def runner(
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        cancel: CancelFlag | None,
    ) -> int:
        del argv, env, timeout, cancel
        nonlocal calls
        calls += 1
        raise RuntimeMissing

    sandbox = ContainerSandbox(
        runtime="docker", image=IMAGE, runner=runner, work_root=tmp_path
    )

    async def once(payload: bytes) -> str:
        report = await sandbox.run(
            execution_id=EXECUTION,
            selector=SELECTOR,
            sha=SHA,
            source_zip=payload,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        return report.reason

    async def scenario() -> tuple[str, str, str]:
        traversed = await once(_zip([("../etc/passwd", b"x")]))
        linked = await once(_zip(link="escape"))
        missing = await once(_zip())
        return traversed, linked, missing

    traversed, linked, missing = asyncio.run(scenario())
    assert traversed == "invalid_source"
    assert linked == "invalid_source"
    assert missing == "runtime_unavailable"
    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_cancel_before_start_skips_the_runtime(tmp_path: Path) -> None:
    async def runner(
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        cancel: CancelFlag | None,
    ) -> int:
        del argv, env, timeout, cancel
        raise AssertionError("runtime was called")

    sandbox = ContainerSandbox(
        runtime="docker", image=IMAGE, runner=runner, work_root=tmp_path
    )
    result = asyncio.run(
        sandbox.run(
            execution_id=EXECUTION,
            selector=SELECTOR,
            sha=SHA,
            source_zip=_zip(),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
            cancel=_Flag(True),
        )
    )
    assert result.status == "cancelled"
    with pytest.raises(ValueError):
        ContainerSandbox(runtime="bash", image=IMAGE)
    with pytest.raises(ValueError):
        ContainerSandbox(runtime="docker", image="python:latest")


def test_reap_removes_only_expired_work_directories(tmp_path: Path) -> None:
    """The reaper deletes leftover directories. It does not call a runtime."""
    old = tmp_path / "fp-old"
    old.mkdir()
    (old / "notes.txt").write_bytes(b"x")
    recent = tmp_path / "fp-new"
    recent.mkdir()
    other = tmp_path / "keep-me"
    other.mkdir()
    link = tmp_path / "fp-link"
    link.symlink_to(old, target_is_directory=True)
    moment = 1_000_000.0
    os.utime(old, (moment - 10_000, moment - 10_000))
    os.utime(recent, (moment, moment))
    removed = reap_workdirs(tmp_path, now=moment, ttl_seconds=3600)
    assert removed == 1
    assert not old.exists()
    assert recent.is_dir()
    assert other.is_dir()
    assert link.is_symlink()
    assert reap_workdirs(tmp_path, now=moment, ttl_seconds=-1) == 0
    assert reap_workdirs(recent / "missing", now=moment, ttl_seconds=1) == 0
