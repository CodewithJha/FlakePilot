import os
import subprocess
import sys

import pytest

from flakepilot.interface.composition_root import Settings, SettingsError


def test_defaults_apply_when_unset_or_empty() -> None:
    assert Settings.from_environ({}) == Settings(
        log_level="INFO", host="127.0.0.1", port=8000
    )
    assert Settings.from_environ({"FLAKEPILOT_PORT": ""}).port == 8000


def test_values_are_read_from_the_environment() -> None:
    environ = {
        "FLAKEPILOT_LOG_LEVEL": "debug",
        "FLAKEPILOT_HOST": "0.0.0.0",
        "FLAKEPILOT_PORT": "9000",
    }
    assert Settings.from_environ(environ) == Settings("DEBUG", "0.0.0.0", 9000)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FLAKEPILOT_LOG_LEVEL", "LOUD-s3cret"),
        ("FLAKEPILOT_HOST", "s3cret.example"),
        ("FLAKEPILOT_PORT", "70000"),
        ("FLAKEPILOT_PORT", "80s3cret"),
    ],
)
def test_invalid_value_names_the_variable_but_not_the_value(
    name: str, value: str
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ({name: value})
    assert name in str(excinfo.value)
    assert value not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_entry_point_fails_fast_on_invalid_settings() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root"],
        env={**os.environ, "FLAKEPILOT_PORT": "80s3cret"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "FLAKEPILOT_PORT" in result.stderr
    assert "80s3cret" not in result.stderr
    assert "Traceback" not in result.stderr
