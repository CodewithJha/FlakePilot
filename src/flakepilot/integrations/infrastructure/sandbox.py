"""Sandbox adapter (ADR-004, ADR-008).

`ClosedSandbox` is the default. It returns `execution_closed` and starts nothing.

`ContainerSandbox` is evaluation-only. It asks `docker` or `podman` to run one
fixed command inside a fresh container. It never executes that command on the
host. If the runtime binary is missing, the result is `runtime_unavailable`.

The container is not a microVM. `--network=none` is what blocks egress and
metadata addresses. A worker killed with SIGKILL can leave a container behind;
there is no TTL reaper.
"""

import asyncio
import io
import os
import re
import stat
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from flakepilot.integrations.application.ports import (
    CancelFlag,
    Sandbox,
    SandboxFile,
    SandboxReport,
)

__all__ = [
    "APPROVED_COMMAND",
    "PNG_MAGIC",
    "CancelFlag",
    "ClosedSandbox",
    "ContainerSandbox",
    "Sandbox",
    "SandboxFile",
    "SandboxReport",
    "image_ok",
    "unverified_source",
]

APPROVED_COMMAND = "/opt/flakepilot/reproduce"
MAX_RESULT_BYTES = 8192
MAX_SCREENSHOT_BYTES = 1_048_576
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,180}@sha256:[0-9a-f]{64}")


def unverified_source() -> SandboxReport:
    """The control plane could not fetch a pinned archive. No process is started."""
    return SandboxReport("infra", "source_unverified")


def image_ok(value: str) -> bool:
    return _IMAGE.fullmatch(value) is not None


_CLIENT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


class RuntimeMissing(Exception):
    """The container runtime binary is not installed. Nothing was executed."""


class Runner(Protocol):
    """Runs the runtime client. Implementations must not use a shell."""

    async def __call__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        cancel: CancelFlag | None,
    ) -> int: ...


class ClosedSandbox:
    """Production default. No container, no subprocess, no repository code."""

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
    ) -> SandboxReport:
        del execution_id, selector, sha, source_zip, wall_seconds, memory_mb, pids
        del cancel
        return SandboxReport("closed", "execution_closed")


class ContainerSandbox:
    def __init__(
        self,
        *,
        runtime: str,
        image: str,
        runner: Runner | None = None,
        work_root: Path | None = None,
    ) -> None:
        if runtime not in ("docker", "podman"):
            raise ValueError("runtime must be docker or podman")
        if _IMAGE.fullmatch(image) is None:
            raise ValueError("image must be digest-pinned")
        self._runtime = runtime
        self._image = image
        self._runner = runner or _subprocess_runner
        self._root = work_root or Path(os.environ.get("TMPDIR", "/tmp"))

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
    ) -> SandboxReport:
        if cancel is not None and cancel.requested():
            return SandboxReport("cancelled", "cancelled")
        work = self._root / f"fp-{uuid.uuid4().hex}"
        source = work / "source"
        output = work / "out"
        try:
            source.mkdir(parents=True, mode=0o700)
            output.mkdir(mode=0o700)
            try:
                _extract(source_zip, source)
            except ValueError:
                return SandboxReport("infra", "invalid_source")
            argv = _argv(
                runtime=self._runtime,
                image=self._image,
                name=f"fp-{execution_id[:8]}",
                source=source,
                output=output,
                selector=selector,
                sha=sha,
                memory_mb=memory_mb,
                pids=pids,
            )
            try:
                code = await self._runner(
                    argv,
                    env=_CLIENT_ENV,
                    timeout=float(wall_seconds),
                    cancel=cancel,
                )
            except RuntimeMissing:
                return SandboxReport("infra", "runtime_unavailable")
            if cancel is not None and cancel.requested():
                return SandboxReport("cancelled", "cancelled")
            if code == 124:
                return SandboxReport("timed_out", "timeout")
            return _collect(output, code)
        finally:
            _remove(work)


def _argv(
    *,
    runtime: str,
    image: str,
    name: str,
    source: Path,
    output: Path,
    selector: str,
    sha: str,
    memory_mb: int,
    pids: int,
) -> list[str]:
    return [
        runtime,
        "run",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        f"--pids-limit={pids}",
        f"--memory={memory_mb}m",
        "--cpus=1",
        "--user=65534:65534",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
        "-v",
        f"{source.resolve()}:/workspace/source:ro",
        "-v",
        f"{output.resolve()}:/workspace/out:rw",
        "-e",
        f"SELECTOR={selector}",
        "-e",
        f"FLAKEPILOT_SHA={sha}",
        image,
        APPROVED_COMMAND,
    ]


def _collect(output: Path, code: int) -> SandboxReport:
    result = output / "result.json"
    if result.is_file() and result.stat().st_size > MAX_RESULT_BYTES:
        return SandboxReport("infra", "output_too_large")
    files: list[SandboxFile] = []
    if result.is_file():
        files.append(SandboxFile("result", result.read_bytes()))
    shot = output / "shot.png"
    if (
        shot.is_file()
        and shot.stat().st_size <= MAX_SCREENSHOT_BYTES
        and shot.read_bytes().startswith(PNG_MAGIC)
    ):
        files.append(SandboxFile("screenshot", shot.read_bytes()))
    if code != 0 and not files:
        return SandboxReport("infra", "runtime_failed")
    return SandboxReport("finished", "finished", tuple(files))


def _extract(data: bytes, dest: Path) -> None:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("invalid_source")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("invalid_source") from None
    total = 0
    files = 0
    root = dest.resolve()
    for info in archive.infolist():
        if info.is_dir():
            continue
        files += 1
        mode = info.external_attr >> 16
        relative = Path(info.filename)
        if (
            files > MAX_ARCHIVE_FILES
            or info.file_size < 0
            or stat.S_ISLNK(mode)
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise ValueError("invalid_source")
        target = (dest / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("invalid_source")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("invalid_source")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with archive.open(info) as src, target.open("wb") as out:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("invalid_source")
                out.write(chunk)
        if info.file_size not in (0, written):
            raise ValueError("invalid_source")


def _remove(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


async def _subprocess_runner(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    cancel: CancelFlag | None,
) -> int:
    """Exec the runtime client with a clean environment. No shell."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeMissing from None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while process.returncode is None:
        if cancel is not None and cancel.requested():
            process.kill()
            await process.wait()
            await _remove_container(argv)
            return 137
        remaining = deadline - loop.time()
        if remaining <= 0:
            process.kill()
            await process.wait()
            await _remove_container(argv)
            return 124
        try:
            await asyncio.wait_for(process.wait(), timeout=min(0.2, remaining))
        except TimeoutError:
            continue
    return process.returncode or 0


async def _remove_container(argv: list[str]) -> None:
    if "--name" not in argv:
        return
    name = argv[argv.index("--name") + 1]
    runtime = argv[0]
    try:
        process = await asyncio.create_subprocess_exec(
            runtime,
            "rm",
            "-f",
            name,
            env=dict(_CLIENT_ENV),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
