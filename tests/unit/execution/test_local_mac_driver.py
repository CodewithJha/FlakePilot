"""The Mac probe must keep a measurement, and must not invent one.

These tests load the driver. They do not start Docker and they do not pass
an isolation check.
"""

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[3]
DRIVER = ROOT / "ops" / "isolation" / "local_mac_driver.py"


class _Client(Protocol):
    def run_argv(
        self, argv: list[str], timeout: float, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]: ...


class _Probe(Protocol):
    DISK_SCRIPT: str
    SHORT_SCRIPT: str
    OOM_SCRIPT: str
    PID_SCRIPT: str
    _disk_status: Callable[[dict[str, str], int], tuple[str, str]]
    _short_status: Callable[[dict[str, str], int], tuple[str, str]]
    _oom_status: Callable[[str, str], tuple[str, str]]
    _pid_status: Callable[[dict[str, str], int], tuple[str, str]]
    _fields: Callable[[str], dict[str, str]]
    _Docker: Callable[[str], _Client]


def _driver() -> _Probe:
    spec = importlib.util.spec_from_file_location("local_mac_driver", DRIVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("driver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_Probe, module)


def test_one_dd_at_the_cap_passes_without_a_thousand_loops() -> None:
    status, _detail = _driver()._disk_status(
        {
            "end": "67108864",
            "extra": "0",
            "loops": "1",
            "mount": "rw_nosuid_nodev_noexec_size=65536k",
        },
        0,
    )
    assert status == "PASS"


def test_a_missing_disk_result_names_the_client_exit() -> None:
    status, detail = _driver()._disk_status({}, 124)
    assert status == "FAIL"
    assert detail == "no_result exit=124"


def test_bytes_past_the_cap_stay_a_failure() -> None:
    status, detail = _driver()._disk_status(
        {"end": "67174400", "extra": "0", "mount": "size=65536k"},
        0,
    )
    assert status == "FAIL"
    assert "67174400" in detail


def test_a_missing_short_result_names_the_client_exit() -> None:
    status, detail = _driver()._short_status({}, 124)
    assert status == "FAIL"
    assert detail == "no_result exit=124"


def test_a_short_fill_stopped_at_the_cap_passes() -> None:
    status, _detail = _driver()._short_status(
        {"end": "67108864", "extra": "0", "short": "1", "last": "28864"},
        0,
    )
    assert status == "PASS"


def test_oom_killed_with_exit_zero_stays_a_failure() -> None:
    status, detail = _driver()._oom_status("true 0", "")
    assert status == "FAIL"
    assert detail == "true 0"


def test_oom_killed_with_exit_137_passes_only_without_a_finished_line() -> None:
    status, _detail = _driver()._oom_status("true 137", "")
    assert status == "PASS"
    swallowed, _detail = _driver()._oom_status("true 137", "oom_finished=1\n")
    assert swallowed == "FAIL"


def test_oom_not_killed_stays_a_failure() -> None:
    status, detail = _driver()._oom_status("false 0", "")
    assert status == "FAIL"
    assert detail == "false 0"


def test_a_missing_pid_count_names_the_client_exit() -> None:
    status, detail = _driver()._pid_status({}, 2)
    assert status == "FAIL"
    assert detail == "no_result exit=2"


def test_a_pid_count_inside_the_limit_passes_and_zero_does_not() -> None:
    module = _driver()
    assert module._pid_status({"pids_ok": "14"}, 0)[0] == "PASS"
    status, detail = module._pid_status({"pids_ok": "0"}, 0)
    assert status == "FAIL"
    assert detail == "pids_ok=0"


def test_forty_pids_means_the_limit_did_not_hold() -> None:
    status, detail = _driver()._pid_status({"pids_ok": "40"}, 0)
    assert status == "FAIL"
    assert detail == "pids_ok=40"


def test_disk_and_short_scripts_use_one_dd() -> None:
    module = _driver()
    assert "while" not in module.DISK_SCRIPT
    assert "bs=65536 count=1025" in module.DISK_SCRIPT
    assert "while" not in module.SHORT_SCRIPT
    assert "bs=60000 count=1200" in module.SHORT_SCRIPT


def test_oom_script_replaces_the_shell_and_does_not_report_finished() -> None:
    script = _driver().OOM_SCRIPT
    assert "exec dd if=/dev/zero of=/tmp/big bs=1048576 count=80" in script
    assert "oom_finished" not in script
    assert "|| true" not in script


def test_pid_forks_inside_a_subshell_and_the_parent_prints_the_count() -> None:
    script = _driver().PID_SCRIPT
    group = script.find("\n(")
    close = script.find("\n)")
    result = script.find("RESULT")
    assert group != -1 and close > group and result > close
    body = script[group:close]
    assert "sleep 30" in body and "&" in body and "while" in body
    parent = script[close + 1 :]
    assert "sleep" not in parent
    assert "&" not in parent
    assert "kill" not in parent
    assert "jobs" not in parent


def test_a_timed_out_client_keeps_the_bytes_already_written() -> None:
    docker = _driver()._Docker("m12345678")
    completed = docker.run_argv(
        [
            sys.executable,
            "-c",
            "print('RESULT end=1', flush=True); import time; time.sleep(30)",
        ],
        1,
    )
    assert completed.returncode == 124
    assert b"RESULT end=1" in completed.stdout


def test_timeout_preserves_bytes_not_a_string() -> None:
    """A late guest line is still bytes the field parser can read."""
    module = _driver()
    docker = module._Docker("m12345678")
    completed = docker.run_argv(
        [
            sys.executable,
            "-c",
            (
                "print('RESULT end=67108864 extra=0 mount=size=65536k', flush=True); "
                "import time; time.sleep(30)"
            ),
        ],
        1,
    )
    assert isinstance(completed.stdout, bytes)
    text = completed.stdout.decode("utf-8")
    status, _detail = module._disk_status(module._fields(text), completed.returncode)
    assert completed.returncode == 124
    assert status == "PASS"
