import os
import subprocess
import sys

import pytest

from flakepilot.interface.composition_root import Secret, Settings, SettingsError

DSN = "postgresql://usr_x1:pw_Q7z@host_k2:5432/db_m3"
DSN_PARTS = ("usr_x1", "pw_Q7z", "host_k2", "db_m3")


def test_defaults_apply_when_unset_or_empty() -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN, "FLAKEPILOT_PORT": ""}
    assert Settings.from_environ(environ) == Settings(
        Secret(DSN), "INFO", "127.0.0.1", 8000
    )


def test_values_are_read_from_the_environment() -> None:
    environ = {
        "FLAKEPILOT_DATABASE_URL": DSN,
        "FLAKEPILOT_LOG_LEVEL": "debug",
        "FLAKEPILOT_HOST": "0.0.0.0",
        "FLAKEPILOT_PORT": "9000",
    }
    settings = Settings.from_environ(environ)
    assert settings == Settings(Secret(DSN), "DEBUG", "0.0.0.0", 9000)
    assert settings.database_url.reveal() == DSN


def test_database_url_never_appears_in_repr_or_str() -> None:
    settings = Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN})
    for text in (repr(settings), str(settings), f"{settings.database_url}"):
        assert not any(part in text for part in DSN_PARTS), text


def test_database_url_is_required() -> None:
    with pytest.raises(SettingsError, match="FLAKEPILOT_DATABASE_URL is required"):
        Settings.from_environ({})


@pytest.mark.parametrize(
    "url",
    [
        "mysql://usr_x1:pw_Q7z@host_k2:5432/db_m3",
        "postgresql://usr_x1:pw_Q7z@:5432/db_m3",
        "postgresql://usr_x1:pw_Q7z@host_k2:5432/",
        "postgresql://usr_x1:pw_Q7z@host_k2:5432/db_m3/extra",
        "postgresql://usr_x1:pw_Q7z@host_k2:99999/db_m3",
        "postgresql://usr_x1:pw_Q7z@[host_k2/db_m3",
    ],
)
def test_invalid_database_url_names_the_variable_but_no_part_of_it(url: str) -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ({"FLAKEPILOT_DATABASE_URL": url})
    message = str(excinfo.value)
    assert message.startswith("FLAKEPILOT_DATABASE_URL ")
    assert not any(part in message for part in DSN_PARTS), message
    assert excinfo.value.__cause__ is None


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
        Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN, name: value})
    assert name in str(excinfo.value)
    assert value not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "FLAKEPILOT_DATABASE_URL is required"),
        (
            {"FLAKEPILOT_DATABASE_URL": DSN, "FLAKEPILOT_PORT": "80s3cret"},
            "FLAKEPILOT_PORT",
        ),
    ],
)
def test_entry_point_fails_fast_on_invalid_settings(
    overrides: dict[str, str], expected: str
) -> None:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    result = subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root"],
        env={**environ, **overrides},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert "80s3cret" not in result.stderr
    assert not any(part in result.stderr for part in DSN_PARTS)
    assert "Traceback" not in result.stderr
