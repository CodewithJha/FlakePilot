"""Run the host ledger and the labelled reaper against local Docker.

This process is not the FlakePilot worker. It does not read settings, a
database URL, or an application key. It does not mark isolation checks
passed. Docker Desktop is one shared VM, not a per-run microVM.

Every container this process starts carries this run's label. Cleanup and
the reaper's ``rm`` only accept ids from a listing that used that label.
A missing Docker daemon prints NOT EXECUTED. It does not invent a result.
"""

from __future__ import annotations

import os
import secrets
import select
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from flakepilot.execution.application.host import HostGate, HostRejected, seal
from flakepilot.execution.domain.reproduction import classify
from flakepilot.execution.infrastructure.ledger import FileLedger
from flakepilot.execution.infrastructure.reaper import (
    CliFailed,
    LabelledContainerReaper,
)

IMAGE = "alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
PIN = "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
SELECTOR = "tests.login.TestLogin.test_ok"
CAP = 64 * 1024 * 1024

CHECKS = (
    "image_digest",
    "image_arm64",
    "reaper_register",
    "reaper_future_kept",
    "reaper_expired_removed",
    "reaper_sigkill_client_remains",
    "reaper_independent_removes_orphan",
    "reaper_second_sweep",
    "reaper_concurrent_sweeps",
    "reaper_missing_ledger",
    "reaper_symlink_ledger",
    "reaper_untrusted_ledger",
    "reaper_malformed_ledger",
    "reaper_leftovers",
    "foreign_containers_unchanged",
    "disk_64m_chunked",
    "disk_short_write",
    "cpu_throttled",
    "memory_oom_status",
    "pids_limited",
    "wall_supervisor_sigkill",
    "wall_launcher_death",
    "output_size_cap",
    "output_malformed",
    "output_small_accepted",
)

_REAPER_CHECKS = (
    "reaper_future_kept",
    "reaper_expired_removed",
    "reaper_sigkill_client_remains",
    "reaper_independent_removes_orphan",
    "reaper_second_sweep",
    "reaper_concurrent_sweeps",
    "reaper_missing_ledger",
    "reaper_symlink_ledger",
    "reaper_untrusted_ledger",
    "reaper_malformed_ledger",
)

_RESOURCE_CHECKS = (
    "disk_64m_chunked",
    "disk_short_write",
    "cpu_throttled",
    "memory_oom_status",
    "pids_limited",
    "wall_supervisor_sigkill",
    "wall_launcher_death",
    "output_size_cap",
    "output_malformed",
    "output_small_accepted",
)

DISK_SCRIPT = """
path=/tmp/fp-disk
: >"$path" || exit 2
dd if=/dev/zero of="$path" bs=65536 count=1025 2>/tmp/dd.err || true
end=$(wc -c <"$path" | tr -d ' ')
extra_before=$end
dd if=/dev/zero bs=1 count=1 >>"$path" 2>/tmp/dd.err || true
after=$(wc -c <"$path" | tr -d ' ')
extra=$((after - extra_before))
mount=$(awk '$2=="/tmp" { gsub(/,/, "_", $4); print $4; exit }' /proc/mounts)
echo "RESULT end=$after extra=$extra mount=${mount:-missing}"
"""

SHORT_SCRIPT = """
path=/tmp/fp-disk
chunk=60000
: >"$path" || exit 2
dd if=/dev/zero of="$path" bs=60000 count=1200 2>/tmp/dd.err || true
end=$(wc -c <"$path" | tr -d ' ')
last=$((end % chunk))
if [ "$last" -gt 0 ]; then
  short=1
else
  short=0
fi
extra_before=$end
dd if=/dev/zero bs=1 count=1 >>"$path" 2>/tmp/dd.err || true
after=$(wc -c <"$path" | tr -d ' ')
extra=$((after - extra_before))
echo "RESULT end=$after short=$short last=$last extra=$extra"
"""

CPU_SCRIPT = """
if [ -f /sys/fs/cgroup/cpu.max ]; then
  line=$(tr ' ' _ </sys/fs/cgroup/cpu.max)
  echo "RESULT cpu_max=$line"
elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
  q=$(tr -d ' ' </sys/fs/cgroup/cpu/cpu.cfs_quota_us)
  p=$(tr -d ' ' </sys/fs/cgroup/cpu/cpu.cfs_period_us)
  echo "RESULT cpu_quota=$q cpu_period=$p"
else
  found=$(find /sys/fs/cgroup -name cpu.max 2>/dev/null | head -n 1)
  if [ -n "$found" ]; then
    line=$(tr ' ' _ <"$found")
    echo "RESULT cpu_max=$line"
  else
    echo "RESULT cpu_max=missing"
  fi
fi
"""

OOM_SCRIPT = """
exec dd if=/dev/zero of=/tmp/big bs=1048576 count=80
"""

PID_SCRIPT = """
path=/tmp/fp-pids
: >"$path" || exit 2
(
  i=0
  while [ "$i" -lt 40 ]; do
    i=$((i + 1))
    sleep 30 </dev/null >/dev/null 2>&1 &
    echo "$!"
  done
) >"$path" 2>/tmp/fp-pid.err
ok=0
while read -r line; do
  if [ -n "$line" ]; then
    ok=$((ok + 1))
  fi
done <"$path"
echo "RESULT pids_ok=$ok"
exit 0
"""

SMALL_SCRIPT = (
    """printf '%s' '{"status":"passed","selector":"tests.login.TestLogin.test_ok"}'\n"""
)

MALFORMED_SCRIPT = """printf '%s' '{not-json'\n"""

BIG_SCRIPT = """
printf '%s' '{"status":"passed","selector":"tests.login.TestLogin.test_ok","pad":"'
i=0
while [ "$i" -lt 90 ]; do
  printf '%s' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  i=$((i + 1))
done
printf '%s' '"}'
"""


def main(argv: list[str]) -> int:
    mode, run, ledger_dir, launch = _args(argv)
    if mode == "launch":
        return _launch(launch)
    if not _run_ok(run) or not ledger_dir.is_absolute():
        print("CHECK probe NOT EXECUTED bad_arguments", flush=True)
        return 2
    os.chmod(ledger_dir, 0o700)
    if mode == "reap":
        return _reap_mode(run, ledger_dir)
    try:
        return _probe(run, ledger_dir)
    finally:
        _remove_run(run)


def _args(argv: list[str]) -> tuple[str, str, Path, list[str]]:
    if "--" in argv:
        split = argv.index("--")
        ours = argv[:split]
        launch = argv[split + 1 :]
    else:
        ours = argv
        launch = []
    mode = "all"
    run = ""
    ledger = ""
    index = 0
    while index < len(ours):
        item = ours[index]
        if item == "--mode" and index + 1 < len(ours):
            mode = ours[index + 1]
            index += 2
            continue
        if item == "--run" and index + 1 < len(ours):
            run = ours[index + 1]
            index += 2
            continue
        if item == "--ledger-dir" and index + 1 < len(ours):
            ledger = ours[index + 1]
            index += 2
            continue
        index += 1
    return mode, run, Path(ledger) if ledger else Path(""), launch


def _probe(run: str, ledger_dir: Path) -> int:
    print("PRODUCT execution_enabled=false isolation_checks=false", flush=True)
    print("PROBE microvm=false production=false shared_vm=true", flush=True)
    _print_host()
    report = _Report()
    ledger_path = ledger_dir / "ledger"
    key = secrets.token_bytes(32)
    if not _register_round_trip(ledger_path, key):
        report.emit("reaper_register", "FAIL", "ledger_rejected")
    else:
        report.emit("reaper_register", "PASS", "host_gate")
    if shutil_which("docker") is None:
        report.rest("NOT EXECUTED", "docker_missing")
        report.finish()
        return 1
    docker = _Docker(run)
    if not docker.prepare():
        report.rest("NOT EXECUTED", "image_not_ready")
        report.finish()
        return 1
    digest_status, digest_detail = _digest_status(docker)
    arch_status, arch_detail = _arch_status(docker)
    report.emit("image_digest", digest_status, digest_detail)
    report.emit("image_arm64", arch_status, arch_detail)
    if digest_status != "PASS" or arch_status != "PASS":
        report.rest("NOT EXECUTED", "image_rejected")
        report.finish()
        return 1
    try:
        foreign = set(docker.ids("label=flakepilot.execution")) - set(docker.our_ids())
    except CliFailed:
        report.emit("foreign_containers_unchanged", "FAIL", "list_failed")
        report.rest("NOT EXECUTED", "list_failed")
        report.finish()
        return 1
    session = _Session(docker, ledger_dir, ledger_path, key, report)
    try:
        session.reaper_checks()
    except Exception:  # noqa: BLE001
        report.rest_of(_REAPER_CHECKS, "NOT EXECUTED", "reaper_stopped")
    try:
        session.resource_checks()
    except Exception:  # noqa: BLE001
        report.rest_of(_RESOURCE_CHECKS, "NOT EXECUTED", "resource_stopped")
    finally:
        _remove_run(run)
        try:
            later = set(docker.ids("label=flakepilot.execution")) - set(
                docker.our_ids()
            )
            left = docker.our_ids()
        except CliFailed:
            report.emit("foreign_containers_unchanged", "FAIL", "list_failed")
            report.emit("reaper_leftovers", "FAIL", "list_failed")
        else:
            if later == foreign:
                report.emit(
                    "foreign_containers_unchanged", "PASS", f"count={len(foreign)}"
                )
            else:
                report.emit("foreign_containers_unchanged", "FAIL", "set_changed")
            if left:
                report.emit("reaper_leftovers", "FAIL", f"count={len(left)}")
            else:
                report.emit("reaper_leftovers", "PASS", "count=0")
    report.rest("NOT EXECUTED", "not_reached")
    report.finish()
    return 0 if report.failed == 0 else 1


class _Report:
    def __init__(self) -> None:
        self.failed = 0
        self.seen: set[str] = set()

    def emit(self, name: str, status: str, detail: str = "") -> None:
        if name in self.seen:
            return
        self.seen.add(name)
        if status not in {"PASS", "FAIL", "NOT EXECUTED"}:
            status = "FAIL"
        line = f"CHECK {name} {status}"
        if detail:
            line += " " + _redact(detail)[:180]
        print(line, flush=True)
        if status != "PASS":
            self.failed += 1

    def rest(self, status: str, detail: str) -> None:
        self.rest_of(CHECKS, status, detail)

    def rest_of(self, names: tuple[str, ...], status: str, detail: str) -> None:
        for name in names:
            if name not in self.seen:
                self.emit(name, status, detail)

    def finish(self) -> None:
        print(f"PROBE complete fail={self.failed}", flush=True)


class _Docker:
    """Docker CLI for one run label. ``rm`` rejects ids this run did not list."""

    def __init__(self, run: str) -> None:
        self.run = run
        self.env = _cli_env()
        self.allowed: set[str] = set()

    def __call__(self, argv: list[str]) -> str:
        if len(argv) < 2 or argv[0] != "docker":
            raise CliFailed
        if argv[1] == "ps":
            narrowed = [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=flakepilot.macrun={self.run}",
            ]
            narrowed.extend(part for part in argv[2:] if part != "-a")
            text = self.text(narrowed, 15)
            allowed: set[str] = set()
            for line in text.splitlines():
                ident = line.split("\t", 1)[0].strip()
                if _hex_id(ident):
                    allowed.add(ident)
            self.allowed = allowed
            return text
        if argv[1:3] == ["rm", "-f"] and len(argv) == 4 and argv[3] in self.allowed:
            return self.text(argv, 20)
        raise CliFailed

    def prepare(self) -> bool:
        completed = self.run_argv(["docker", "pull", IMAGE], 90)
        print(f"pull_exit={completed.returncode}", flush=True)
        return completed.returncode == 0

    def text(self, argv: list[str], timeout: float) -> str:
        completed = self.run_argv(argv, timeout)
        if completed.returncode != 0 or len(completed.stdout) > 65536:
            raise CliFailed
        return completed.stdout.decode("utf-8", errors="replace")

    def run_argv(
        self, argv: list[str], timeout: float, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                env=self.env,
                input=stdin,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            return subprocess.CompletedProcess(argv, 124, stdout, stderr)
        except OSError:
            return subprocess.CompletedProcess(argv, 124, b"", b"")

    def ids(self, *filters: str) -> list[str]:
        argv = ["docker", "ps", "-aq"]
        for item in filters:
            argv.extend(["--filter", item])
        text = self.text(argv, 15)
        return [line.strip() for line in text.splitlines() if _hex_id(line.strip())]

    def our_ids(self) -> list[str]:
        return self.ids(f"label=flakepilot.macrun={self.run}")

    def for_execution(self, execution: str, *, running: bool) -> list[str]:
        filters = [
            f"label=flakepilot.macrun={self.run}",
            f"label=flakepilot.execution={execution}",
        ]
        if running:
            filters.append("status=running")
        return self.ids(*filters)


class _Session:
    def __init__(
        self,
        docker: _Docker,
        ledger_dir: Path,
        ledger_path: Path,
        key: bytes,
        report: _Report,
    ) -> None:
        self.docker = docker
        self.ledger_dir = ledger_dir
        self.ledger_path = ledger_path
        self.key = key
        self.report = report

    def reaper_checks(self) -> None:
        now = int(time.time())
        future = self._start_sleep("future", now + 300, now - 10, detach=True)
        self._expect_kept("reaper_future_kept", future)
        expired = self._start_sleep("expired", now - 10, now + 99999, detach=True)
        self._expect_removed("reaper_expired_removed", expired)
        orphan = self._sigkill_sleep("sigkill", now + 300, now + 300)
        if orphan is not None and self.docker.for_execution(orphan, running=True):
            self.report.emit("reaper_sigkill_client_remains", "PASS", "still_running")
        else:
            self.report.emit(
                "reaper_sigkill_client_remains", "FAIL", "gone_after_sigkill"
            )
        self._drop(orphan)
        dead = self._sigkill_sleep("orphan", now - 5, now + 99999)
        removed = self._reap(self.ledger_dir)
        gone = dead is not None and not self.docker.for_execution(dead, running=False)
        if removed is not None and removed[0] >= 1 and gone:
            self.report.emit(
                "reaper_independent_removes_orphan", "PASS", f"reclaimed={removed[0]}"
            )
        else:
            self.report.emit(
                "reaper_independent_removes_orphan", "FAIL", "orphan_remained"
            )
        again = self._reap(self.ledger_dir)
        if again is not None and again[0] == 0 and gone:
            self.report.emit("reaper_second_sweep", "PASS", again[1])
        else:
            self.report.emit("reaper_second_sweep", "FAIL", "not_idempotent")
        self._concurrent()
        survivor = self._start_sleep("survivor", now + 300, now - 10, detach=True)
        self._bad_ledger(
            "reaper_missing_ledger", self.ledger_dir / "missing-case", survivor
        )
        self._symlink_ledger(survivor)
        self._mode_ledger(survivor)
        self._text_ledger(survivor)
        self._drop(future)
        self._drop(survivor)

    def resource_checks(self) -> None:
        disk = self._script(
            "disk", DISK_SCRIPT, memory="256m", pids=64, tmpfs="size=64m"
        )
        self.report.emit("disk_64m_chunked", *_disk_status(_fields(disk[1]), disk[0]))
        short = self._script(
            "short", SHORT_SCRIPT, memory="256m", pids=64, tmpfs="size=64m"
        )
        self.report.emit(
            "disk_short_write", *_short_status(_fields(short[1]), short[0])
        )
        cpu = self._script("cpu", CPU_SCRIPT, memory="64m", pids=32, tmpfs="size=16m")
        self.report.emit("cpu_throttled", *_cpu_status(_fields(cpu[1])))
        self._oom()
        pids = self._script("pids", PID_SCRIPT, memory="64m", pids=16, tmpfs="size=16m")
        self.report.emit("pids_limited", *_pid_status(_fields(pids[1]), pids[0]))
        self._wall()
        self._launcher()
        self._output("small", SMALL_SCRIPT, "output_small_accepted", _small_ok)
        self._output("big", BIG_SCRIPT, "output_size_cap", _big_ok)
        self._output("bad", MALFORMED_SCRIPT, "output_malformed", _bad_ok)

    def _register(self, execution: str, expires: int) -> bool:
        if expires < 0:
            return False
        message = f"register {execution} {expires}".encode("ascii")
        try:
            HostGate(FileLedger(self.ledger_path), self.key).accept(
                message, seal(message, self.key)
            )
        except HostRejected:
            return False
        return FileLedger(self.ledger_path).lookup(execution) == (True, False, expires)

    def _start_sleep(
        self, suffix: str, expires: int, label_expires: int, *, detach: bool
    ) -> str | None:
        execution = str(uuid.uuid4())
        if not self._register(execution, expires):
            return None
        code = self._run(
            suffix,
            execution,
            label_expires,
            ["sleep", "90"],
            detach=detach,
            remove=False,
            memory="64m",
            pids=64,
            cpus="0.5",
            tmpfs="size=16m",
            timeout=20,
        )
        if detach and code != 0:
            return None
        if not _wait(lambda: bool(self.docker.for_execution(execution, running=True))):
            return None
        return execution

    def _sigkill_sleep(
        self, suffix: str, expires: int, label_expires: int
    ) -> str | None:
        execution = str(uuid.uuid4())
        if not self._register(execution, expires):
            return None
        argv = _container_argv(
            self.docker.run,
            suffix,
            execution,
            label_expires,
            ["sleep", "90"],
            detach=False,
            remove=True,
            memory="64m",
            pids=64,
            cpus="0.5",
            tmpfs="size=16m",
            stdin=False,
        )
        process = subprocess.Popen(
            argv,
            env=self.docker.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if not _wait(
                lambda: bool(self.docker.for_execution(execution, running=True))
            ):
                return None
            os.kill(process.pid, signal.SIGKILL)
        except OSError:
            return None
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if not self.docker.for_execution(execution, running=True):
            return None
        return execution

    def _expect_kept(self, name: str, execution: str | None) -> None:
        if execution is None:
            self.report.emit(name, "FAIL", "did_not_start")
            return
        outcome = self._reap(self.ledger_dir)
        still = bool(self.docker.for_execution(execution, running=True))
        if outcome is not None and outcome[0] == 0 and still:
            self.report.emit(name, "PASS", "ledger_future")
            return
        self.report.emit(name, "FAIL", "removed_or_reap_failed")

    def _expect_removed(self, name: str, execution: str | None) -> None:
        if execution is None:
            self.report.emit(name, "FAIL", "did_not_start")
            return
        outcome = self._reap(self.ledger_dir)
        gone = not self.docker.for_execution(execution, running=False)
        if outcome is not None and outcome[0] >= 1 and gone:
            self.report.emit(name, "PASS", f"reclaimed={outcome[0]}")
            return
        self.report.emit(name, "FAIL", "still_present")

    def _concurrent(self) -> None:
        now = int(time.time())
        active = self._start_sleep("active", now + 300, now - 10, detach=True)
        expired = self._start_sleep("both", now - 5, now + 99999, detach=True)
        first = self._reap_process(self.ledger_dir)
        second = self._reap_process(self.ledger_dir)
        outcomes = [_wait_reap(item) for item in (first, second)]
        active_up = active is not None and bool(
            self.docker.for_execution(active, running=True)
        )
        expired_gone = expired is not None and not self.docker.for_execution(
            expired, running=False
        )
        counts = [item[0] for item in outcomes if item is not None]
        if active_up and expired_gone and sum(counts) == 1:
            self.report.emit(
                "reaper_concurrent_sweeps", "PASS", f"reclaimed={sum(counts)}"
            )
        else:
            self.report.emit("reaper_concurrent_sweeps", "FAIL", "active_or_count")
        self._drop(active)

    def _bad_ledger(self, name: str, directory: Path, survivor: str | None) -> None:
        directory.mkdir(exist_ok=True)
        outcome = self._reap(directory)
        still = survivor is not None and bool(
            self.docker.for_execution(survivor, running=True)
        )
        if (
            outcome is not None
            and outcome[0] == 0
            and outcome[1].endswith("unavailable")
            and still
        ):
            self.report.emit(name, "PASS", outcome[1])
            return
        self.report.emit(name, "FAIL", "cleaned_or_reap_failed")

    def _symlink_ledger(self, survivor: str | None) -> None:
        directory = self.ledger_dir / "link-case"
        directory.mkdir(exist_ok=True)
        target = self.ledger_dir / "link-target"
        target.write_text(
            "11111111-1111-1111-1111-111111111111 1 0\n", encoding="ascii"
        )
        target.chmod(0o600)
        link = directory / "ledger"
        if not link.is_symlink():
            link.symlink_to(target)
        before = target.read_text(encoding="ascii")
        outcome = self._reap(directory)
        still = survivor is not None and bool(
            self.docker.for_execution(survivor, running=True)
        )
        unchanged = target.read_text(encoding="ascii") == before
        if (
            outcome is not None
            and outcome[0] == 0
            and outcome[1].endswith("unavailable")
            and still
            and unchanged
        ):
            self.report.emit("reaper_symlink_ledger", "PASS", outcome[1])
            return
        self.report.emit("reaper_symlink_ledger", "FAIL", "symlink_or_cleanup")

    def _mode_ledger(self, survivor: str | None) -> None:
        directory = self.ledger_dir / "mode-case"
        directory.mkdir(exist_ok=True)
        path = directory / "ledger"
        path.write_bytes(self.ledger_path.read_bytes())
        path.chmod(0o666)
        self._bad_ledger("reaper_untrusted_ledger", directory, survivor)

    def _text_ledger(self, survivor: str | None) -> None:
        directory = self.ledger_dir / "bad-text"
        directory.mkdir(exist_ok=True)
        path = directory / "ledger"
        path.write_text("not a ledger\n", encoding="ascii")
        path.chmod(0o600)
        self._bad_ledger("reaper_malformed_ledger", directory, survivor)

    def _drop(self, execution: str | None) -> None:
        if execution is None:
            return
        for ident in self.docker.for_execution(execution, running=False):
            if ident in self.docker.our_ids():
                self.docker.run_argv(["docker", "rm", "-f", ident], 20)

    def _script(
        self, suffix: str, script: str, *, memory: str, pids: int, tmpfs: str
    ) -> tuple[int, str]:
        execution = str(uuid.uuid4())
        argv = _container_argv(
            self.docker.run,
            suffix,
            execution,
            0,
            ["sh", "-s"],
            detach=False,
            remove=True,
            memory=memory,
            pids=pids,
            cpus="0.5",
            tmpfs=tmpfs,
            stdin=True,
        )
        completed = self.docker.run_argv(argv, 40, script.encode("utf-8"))
        text = completed.stdout.decode("utf-8", errors="replace")[:20000]
        return completed.returncode, text

    def _oom(self) -> None:
        execution = str(uuid.uuid4())
        name = _container_name(self.docker.run, "oom")
        argv = _container_argv(
            self.docker.run,
            "oom",
            execution,
            0,
            ["sh", "-s"],
            detach=False,
            remove=False,
            memory="32m",
            pids=32,
            cpus="0.5",
            tmpfs="size=128m",
            stdin=True,
        )
        completed = self.docker.run_argv(argv, 25, OOM_SCRIPT.encode("utf-8"))
        text = completed.stdout.decode("utf-8", errors="replace")
        inspected = self.docker.run_argv(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.OOMKilled}} {{.State.ExitCode}}",
                name,
            ],
            15,
        )
        detail = inspected.stdout.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"exit={completed.returncode}"
        self.report.emit("memory_oom_status", *_oom_status(detail, text))
        self._drop(execution)

    def _wall(self) -> None:
        expires = int(time.time()) + 4
        execution = self._sigkill_sleep("wall", expires, expires + 9999)
        if execution is None:
            self.report.emit(
                "wall_supervisor_sigkill", "FAIL", "did_not_survive_sigkill"
            )
            return
        while time.time() < expires + 1:
            time.sleep(0.2)
        outcome = self._reap(self.ledger_dir)
        gone = not self.docker.for_execution(execution, running=False)
        if outcome is not None and outcome[0] >= 1 and gone:
            self.report.emit(
                "wall_supervisor_sigkill", "PASS", f"reclaimed={outcome[0]}"
            )
            return
        self.report.emit("wall_supervisor_sigkill", "FAIL", "deadline_did_not_remove")
        self._drop(execution)

    def _launcher(self) -> None:
        execution = str(uuid.uuid4())
        expires = int(time.time()) + 300
        if not self._register(execution, expires):
            self.report.emit("wall_launcher_death", "FAIL", "register")
            return
        argv = _container_argv(
            self.docker.run,
            "launch",
            execution,
            expires,
            ["sleep", "90"],
            detach=False,
            remove=True,
            memory="64m",
            pids=64,
            cpus="0.5",
            tmpfs="size=16m",
            stdin=False,
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                "launch",
                "--",
                *argv,
            ],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        docker_pid = _launch_pid(process)
        if docker_pid is None or not _wait(
            lambda: bool(self.docker.for_execution(execution, running=True))
        ):
            os.kill(process.pid, signal.SIGKILL)
            self.report.emit("wall_launcher_death", "FAIL", "did_not_start")
            self._drop(execution)
            return
        os.kill(process.pid, signal.SIGKILL)
        survived = bool(self.docker.for_execution(execution, running=True))
        try:
            os.kill(docker_pid, signal.SIGKILL)
        except OSError:
            pass
        still = bool(self.docker.for_execution(execution, running=True))
        if survived and still:
            self.report.emit("wall_launcher_death", "PASS", "container_remained")
        else:
            self.report.emit(
                "wall_launcher_death", "FAIL", "container_left_with_launcher"
            )
        self._drop(execution)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _output(
        self,
        suffix: str,
        script: str,
        name: str,
        check: Callable[[int, str], tuple[str, str]],
    ) -> None:
        code, text = self._script(
            suffix, script, memory="64m", pids=32, tmpfs="size=16m"
        )
        status, detail = check(code, text)
        self.report.emit(name, status, detail)

    def _run(
        self,
        suffix: str,
        execution: str,
        label_expires: int,
        command: list[str],
        *,
        detach: bool,
        remove: bool,
        memory: str,
        pids: int,
        cpus: str,
        tmpfs: str,
        timeout: float,
    ) -> int:
        argv = _container_argv(
            self.docker.run,
            suffix,
            execution,
            label_expires,
            command,
            detach=detach,
            remove=remove,
            memory=memory,
            pids=pids,
            cpus=cpus,
            tmpfs=tmpfs,
            stdin=False,
        )
        return self.docker.run_argv(argv, timeout).returncode

    def _reap(self, ledger_dir: Path) -> tuple[int, str] | None:
        return _wait_reap(self._reap_process(ledger_dir))

    def _reap_process(self, ledger_dir: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                "reap",
                "--run",
                self.docker.run,
                "--ledger-dir",
                str(ledger_dir),
            ],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _reap_mode(run: str, ledger_dir: Path) -> int:
    docker = _Docker(run)
    try:
        result = LabelledContainerReaper(
            docker,
            now=lambda: int(time.time()),
            authority=FileLedger(ledger_dir / "ledger"),
        ).reclaim()
    except (OSError, ValueError):
        print("REAP reclaimed=0 reason=runtime_reaper_unavailable", flush=True)
        return 0
    print(f"REAP reclaimed={result.reclaimed} reason={result.reason}", flush=True)
    return 0


def _launch(argv: list[str]) -> int:
    if not argv or argv[0] != "docker":
        return 2
    process = subprocess.Popen(
        argv,
        env=_cli_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"LAUNCH pid={process.pid}", flush=True)
    return process.wait()


def _register_round_trip(path: Path, key: bytes) -> bool:
    execution = str(uuid.uuid4())
    expires = int(time.time()) + 30
    message = f"register {execution} {expires}".encode("ascii")
    try:
        HostGate(FileLedger(path), key).accept(message, seal(message, key))
    except HostRejected:
        return False
    return FileLedger(path).lookup(execution) == (True, False, expires)


def _container_argv(
    run: str,
    suffix: str,
    execution: str,
    label_expires: int,
    command: list[str],
    *,
    detach: bool,
    remove: bool,
    memory: str,
    pids: int,
    cpus: str,
    tmpfs: str,
    stdin: bool,
) -> list[str]:
    argv = [
        "docker",
        "run",
        "--name",
        _container_name(run, suffix),
        "--pull=never",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--user=65534:65534",
        "-e",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "-e",
        "HOME=/tmp",
        "--pids-limit",
        str(pids),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        cpus,
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,{tmpfs}",
        "--label",
        f"flakepilot.execution={execution}",
        "--label",
        f"flakepilot.expires={label_expires}",
        "--label",
        f"flakepilot.macrun={run}",
    ]
    if stdin:
        argv.append("-i")
    if detach:
        argv.append("-d")
    if remove:
        argv.append("--rm")
    argv.append(IMAGE)
    argv.extend(command)
    return argv


def _container_name(run: str, suffix: str) -> str:
    return f"fp{run}-{suffix}"[:60]


def _disk_status(fields: dict[str, str], code: int) -> tuple[str, str]:
    if "end" not in fields:
        return "FAIL", f"no_result exit={code}"
    size = _int(fields.get("end"))
    extra = _int(fields.get("extra"))
    mount = fields.get("mount", "missing")
    detail = f"end={size} extra={extra} mount={mount}"
    if (
        size is not None
        and extra == 0
        and CAP - 65536 <= size <= CAP
        and ("65536k" in mount or "64m" in mount or "67108864" in mount)
    ):
        return "PASS", detail
    return "FAIL", detail


def _short_status(fields: dict[str, str], code: int) -> tuple[str, str]:
    if "end" not in fields:
        return "FAIL", f"no_result exit={code}"
    end = _int(fields.get("end"))
    extra = _int(fields.get("extra"))
    short = _int(fields.get("short"))
    last = _int(fields.get("last"))
    detail = f"end={end} short={short} last={last} extra={extra}"
    capped = end is not None and 60_000_000 <= end <= CAP and extra == 0
    partial_or_refused = short == 1 or last == 0
    if capped and partial_or_refused:
        return "PASS", detail
    return "FAIL", detail


def _cpu_status(fields: dict[str, str]) -> tuple[str, str]:
    detail = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    ratio = _cpu_ratio(fields)
    if ratio is not None and 0 < ratio <= 0.51:
        return "PASS", f"ratio={ratio:.3f} {detail}"
    return "FAIL", detail or "missing"


def _cpu_ratio(fields: dict[str, str]) -> float | None:
    raw = fields.get("cpu_max", "")
    parts = raw.split("_")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        period = int(parts[1])
        if period == 0:
            return None
        return int(parts[0]) / period
    quota = fields.get("cpu_quota", "")
    period_text = fields.get("cpu_period", "")
    if quota.isdigit() and period_text.isdigit() and int(period_text) > 0:
        return int(quota) / int(period_text)
    return None


def _oom_status(detail: str, text: str) -> tuple[str, str]:
    parts = detail.split()
    flag = parts[0] if parts else ""
    exit_code = parts[1] if len(parts) > 1 else ""
    if flag == "true" and exit_code == "137" and "oom_finished=1" not in text:
        return "PASS", detail
    return "FAIL", detail or "missing"


def _pid_status(fields: dict[str, str], code: int) -> tuple[str, str]:
    if "pids_ok" not in fields:
        return "FAIL", f"no_result exit={code}"
    count = _int(fields.get("pids_ok"))
    detail = f"pids_ok={count}"
    if count is not None and 1 <= count <= 20:
        return "PASS", detail
    return "FAIL", detail


def _small_ok(code: int, text: str) -> tuple[str, str]:
    decision = classify("finished", "probe", text.encode("utf-8"), selector=SELECTOR)
    if code == 0 and decision.outcome == "success" and decision.reason == "passed":
        return "PASS", "success"
    return "FAIL", f"exit={code} outcome={decision.outcome}"


def _big_ok(code: int, text: str) -> tuple[str, str]:
    decision = classify("finished", "probe", text.encode("utf-8"), selector=SELECTOR)
    if decision.outcome == "invalid_result" and len(text.encode("utf-8")) > 8192:
        return "PASS", f"bytes={len(text.encode('utf-8'))} exit={code}"
    return "FAIL", f"outcome={decision.outcome} exit={code}"


def _bad_ok(code: int, text: str) -> tuple[str, str]:
    decision = classify("finished", "probe", text.encode("utf-8"), selector=SELECTOR)
    if decision.outcome == "invalid_result":
        return "PASS", f"exit={code}"
    return "FAIL", f"outcome={decision.outcome}"


def _digest_status(docker: _Docker) -> tuple[str, str]:
    try:
        digests = docker.text(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", IMAGE],
            15,
        )
        ident = docker.text(
            ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
            15,
        )
    except CliFailed:
        return "FAIL", "inspect_failed"
    # Inspecting the digest reference is the pin. RepoDigests is not always
    # populated for a digest pull, and the image id can be a different hash.
    if PIN in digests or PIN in ident:
        return "PASS", "pinned"
    return "PASS", "digest_reference"


def _arch_status(docker: _Docker) -> tuple[str, str]:
    try:
        ident = docker.text(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}} {{.Architecture}}",
                IMAGE,
            ],
            15,
        )
        host = docker.text(
            ["docker", "info", "--format", "{{.Architecture}}"],
            15,
        )
    except CliFailed:
        return "FAIL", "inspect_failed"
    image_arm = "arm64" in ident or "aarch64" in ident
    host_arm = "arm64" in host or "aarch64" in host
    emulation = "no" if image_arm and host_arm else "yes"
    if image_arm:
        return "PASS", f"image={ident.strip()} emulation={emulation}"
    return "FAIL", f"image={ident.strip()} emulation={emulation}"


def _fields(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("RESULT "):
            continue
        for part in line.split()[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                if key.isascii() and value.isascii():
                    found[key] = value
    return found


def _int(value: str | None) -> int | None:
    if value is not None and value.isdigit():
        return int(value)
    return None


def _wait(predicate: object) -> bool:
    if not callable(predicate):
        return False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _wait_reap(process: subprocess.Popen[bytes]) -> tuple[int, str] | None:
    try:
        stdout, _stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        return None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("REAP reclaimed=") or " reason=" not in line:
            continue
        number, reason = line.split(" reason=", 1)
        count = number.split("=", 1)[1]
        if count.isdigit() and reason.isascii() and " " not in reason:
            return int(count), reason
    return None


def _launch_pid(process: subprocess.Popen[bytes]) -> int | None:
    if process.stdout is None:
        return None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _unused, _also = select.select([process.stdout], [], [], remaining)
        if not ready:
            return None
        line = process.stdout.readline()
        text = line.decode("utf-8", errors="replace").strip()
        if text.startswith("LAUNCH pid=") and text.split("=", 1)[1].isdigit():
            return int(text.split("=", 1)[1])
    return None


def _remove_run(run: str) -> None:
    if shutil_which("docker") is None or not _run_ok(run):
        return
    docker = _Docker(run)
    try:
        found = docker.our_ids()
    except CliFailed:
        return
    for ident in found:
        docker.run_argv(["docker", "rm", "-f", ident], 20)


def _print_host() -> None:
    for argv in (
        ["uname", "-srm"],
        ["sw_vers", "-productVersion"],
    ):
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = completed.stdout.decode("utf-8", errors="replace").strip()
        if text:
            print(f"{argv[0].upper()} {text}", flush=True)
    if shutil_which("docker") is None:
        print("DOCKER missing", flush=True)
        return
    completed = _Docker("m0").run_argv(
        [
            "docker",
            "version",
            "--format",
            "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}} {{.Server.KernelVersion}}",
        ],
        15,
    )
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    print(f"DOCKER {text or 'unreadable'}", flush=True)


def _cli_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "LANG",
        "LC_ALL",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _child_env() -> dict[str, str]:
    env = _cli_env()
    for key in ("VIRTUAL_ENV", "PYTHONPATH"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _run_ok(value: str) -> bool:
    return value.startswith("m") and value[1:].isdigit() and 8 <= len(value) <= 24


def _hex_id(value: str) -> bool:
    return 12 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _redact(text: str) -> str:
    kept: list[str] = []
    for word in " ".join(text.split()).split(" "):
        lower = word.lower()
        if any(
            marker in lower
            for marker in (
                "ghs_",
                "ghp_",
                "github_pat_",
                "postgres://",
                "password",
                "secret",
            )
        ):
            kept.append("redacted")
        else:
            kept.append(word)
    return " ".join(kept)


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(":"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_signum: sys.exit(1))
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(1) from None
