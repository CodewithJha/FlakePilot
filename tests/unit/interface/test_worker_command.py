"""The worker is a CLI command. The API module does not start it."""

import sys
from pathlib import Path

import pytest

import flakepilot.interface.composition_root.__main__ as entry

DSN = "postgresql://usr_x1:pw_Q7z@host_k2:5432/db_m3"
ROOT = Path(__file__).resolve().parents[3]


def test_api_lifespan_does_not_reference_the_worker() -> None:
    source = (
        ROOT / "src/flakepilot/interface/composition_root/__init__.py"
    ).read_text()
    assert "run_worker" not in source
    assert "build_worker" not in source


def test_worker_subcommand_dispatches_without_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLAKEPILOT_DATABASE_URL", DSN)
    monkeypatch.setattr(sys, "argv", ["flakepilot", "worker"])

    def fake_run(coro: object) -> int:
        close = getattr(coro, "close", None)
        assert close is not None
        close()
        return 0

    monkeypatch.setattr(
        "flakepilot.interface.composition_root.__main__.asyncio.run", fake_run
    )
    with pytest.raises(SystemExit) as excinfo:
        entry.main()
    assert excinfo.value.code == 0


def test_unknown_command_prints_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKEPILOT_DATABASE_URL", DSN)
    monkeypatch.setattr(sys, "argv", ["flakepilot", "worker", "extra"])
    with pytest.raises(SystemExit) as excinfo:
        entry.main()
    assert excinfo.value.code == entry._USAGE


def test_worker_sources_do_not_execute_code_or_call_a_model() -> None:
    paths = (
        ROOT / "src/flakepilot/orchestration/application/worker.py",
        ROOT / "src/flakepilot/interface/composition_root/worker.py",
    )
    source = "\n".join(path.read_text() for path in paths)
    for banned in ("subprocess", "NullAiAssist", "FakeAiAssist", "socket", "grant"):
        assert banned not in source
