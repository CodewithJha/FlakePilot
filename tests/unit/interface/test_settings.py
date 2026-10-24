import base64
import os
import re
import subprocess
import sys
from pathlib import Path

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


GITHUB = {
    "FLAKEPILOT_DATABASE_URL": DSN,
    "FLAKEPILOT_PUBLIC_ORIGIN": "https://FlakePilot.Example:8443/",
    "FLAKEPILOT_GITHUB_CLIENT_ID": "Iv1.abc123",
    "FLAKEPILOT_GITHUB_CLIENT_SECRET": "gh-secret-SENTINEL-9",
    "FLAKEPILOT_SECRET_KEY": base64.b64encode(b"k" * 32).decode(),
}


def test_sign_in_settings_are_read_and_never_revealed() -> None:
    settings = Settings.from_environ(GITHUB)
    assert settings.public_origin == "https://flakepilot.example:8443"
    assert settings.github_app is not None
    assert settings.github_app.client_id == "Iv1.abc123"
    assert settings.github_app.client_secret.reveal() == "gh-secret-SENTINEL-9"
    assert settings.secure_cookies
    assert "SENTINEL" not in repr(settings) and "a2tr" not in repr(settings)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"FLAKEPILOT_GITHUB_CLIENT_SECRET": ""}, "FLAKEPILOT_GITHUB_CLIENT_SECRET is"),
        ({"FLAKEPILOT_GITHUB_CLIENT_ID": ""}, "FLAKEPILOT_GITHUB_CLIENT_ID is"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": ""}, "FLAKEPILOT_PUBLIC_ORIGIN is required"),
        ({"FLAKEPILOT_GITHUB_CLIENT_ID": "id with spaces"}, "not a valid client ID"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "http://fp.example"}, "must use https"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "https://fp.example/app"}, "only scheme"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "https://u:p@fp.example"}, "only scheme"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "https://:p@fp.example"}, "only scheme"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "https://@fp.example"}, "only scheme"),
        ({"FLAKEPILOT_PUBLIC_ORIGIN": "ftp://fp.example"}, "http(s) origin"),
        ({"FLAKEPILOT_ALLOW_INSECURE_COOKIES": "yes"}, "must be true or false"),
        ({"FLAKEPILOT_SECRET_KEY": "not base64!"}, "not valid base64"),
        ({"FLAKEPILOT_SECRET_KEY": base64.b64encode(b"short").decode()}, "32 bytes"),
    ],
)
def test_invalid_sign_in_settings_fail_closed(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ({**GITHUB, **overrides})
    assert message in str(excinfo.value)
    assert "SENTINEL" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_plain_http_is_only_allowed_with_insecure_cookies() -> None:
    settings = Settings.from_environ(
        {
            **GITHUB,
            "FLAKEPILOT_PUBLIC_ORIGIN": "http://localhost:8000",
            "FLAKEPILOT_ALLOW_INSECURE_COOKIES": "true",
        }
    )
    assert settings.public_origin == "http://localhost:8000"
    assert not settings.secure_cookies


@pytest.mark.parametrize(
    ("value", "insecure", "origin"),
    [
        ("https://FP.Example:443", "false", "https://fp.example"),
        ("https://fp.example:8443", "false", "https://fp.example:8443"),
        ("http://LocalHost:80/", "true", "http://localhost"),
        ("http://localhost:443", "true", "http://localhost:443"),
        ("https://[::1]:443", "false", "https://[::1]"),
    ],
)
def test_public_origin_is_normalised_as_browsers_send_it(
    value: str, insecure: str, origin: str
) -> None:
    settings = Settings.from_environ(
        {
            **GITHUB,
            "FLAKEPILOT_PUBLIC_ORIGIN": value,
            "FLAKEPILOT_ALLOW_INSECURE_COOKIES": insecure,
        }
    )
    assert settings.public_origin == origin


def test_sign_in_is_off_without_github_settings() -> None:
    settings = Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN})
    assert settings.github_app is None and settings.secret_key is None


def test_artifact_dir_must_be_an_existing_writable_absolute_directory(
    tmp_path: Path,
) -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN}
    settings = Settings.from_environ(
        {**environ, "FLAKEPILOT_ARTIFACT_DIR": str(tmp_path)}
    )
    assert settings.artifact_dir == tmp_path
    assert Settings.from_environ(environ).artifact_dir is None
    a_file = tmp_path / "file"
    a_file.write_text("x")
    read_only = tmp_path / "read-only"
    read_only.mkdir(mode=0o500)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    group_writable = tmp_path / "group-writable"
    group_writable.mkdir()
    group_writable.chmod(0o770)
    for value in (
        "relative/dir",
        str(tmp_path / "missing"),
        str(a_file),
        str(read_only),
        str(shared),
        str(group_writable),
    ):
        with pytest.raises(SettingsError) as excinfo:
            Settings.from_environ({**environ, "FLAKEPILOT_ARTIFACT_DIR": value})
        assert "FLAKEPILOT_ARTIFACT_DIR" in str(excinfo.value)
        assert value not in str(excinfo.value)


def test_worker_limits_default_and_reject_out_of_range() -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN}
    settings = Settings.from_environ(environ)
    assert (
        settings.worker_poll_seconds,
        settings.worker_batch_size,
        settings.worker_lease_seconds,
    ) == (2, 10, 30)
    tuned = Settings.from_environ(
        {
            **environ,
            "FLAKEPILOT_WORKER_POLL_SECONDS": "5",
            "FLAKEPILOT_WORKER_BATCH_SIZE": "",
            "FLAKEPILOT_WORKER_LEASE_SECONDS": "60",
        }
    )
    assert tuned.worker_poll_seconds == 5
    assert tuned.worker_batch_size == 10
    assert tuned.worker_lease_seconds == 60
    for name, value in (
        ("FLAKEPILOT_WORKER_POLL_SECONDS", "0"),
        ("FLAKEPILOT_WORKER_BATCH_SIZE", "101"),
        ("FLAKEPILOT_WORKER_LEASE_SECONDS", "nope"),
    ):
        with pytest.raises(SettingsError) as excinfo:
            Settings.from_environ({**environ, name: value})
        assert name in str(excinfo.value)
        if not value.isdigit():
            assert value not in str(excinfo.value)


def test_execution_defaults_closed_and_evaluation_needs_a_digest() -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN}
    settings = Settings.from_environ(environ)
    assert settings.execution_mode == "closed"
    assert settings.execution_image is None
    image = "ghcr.io/example/reproduce@sha256:" + ("ab" * 32)
    evaluation = Settings.from_environ(
        {
            **environ,
            "FLAKEPILOT_EXECUTION_MODE": "evaluation",
            "FLAKEPILOT_EXECUTION_IMAGE": image,
        }
    )
    assert evaluation.execution_mode == "evaluation"
    assert evaluation.execution_image == image
    for name, value in (
        ("FLAKEPILOT_EXECUTION_MODE", "host"),
        ("FLAKEPILOT_EXECUTION_IMAGE", "python:latest"),
    ):
        with pytest.raises(SettingsError) as excinfo:
            Settings.from_environ({**environ, name: value})
        assert name in str(excinfo.value)
        assert value not in str(excinfo.value)
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ({**environ, "FLAKEPILOT_EXECUTION_MODE": "evaluation"})
    assert "FLAKEPILOT_EXECUTION_IMAGE" in str(excinfo.value)


def test_contents_token_stays_out_of_errors_and_repr() -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN}
    assert Settings.from_environ(environ).github_contents_token is None
    token = "ghs_" + ("a" * 20)
    settings = Settings.from_environ(
        {**environ, "FLAKEPILOT_GITHUB_CONTENTS_TOKEN": token}
    )
    assert settings.github_contents_token is not None
    assert settings.github_contents_token.reveal() == token
    assert token not in repr(settings)
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ(
            {**environ, "FLAKEPILOT_GITHUB_CONTENTS_TOKEN": "bad token"}
        )
    assert "FLAKEPILOT_GITHUB_CONTENTS_TOKEN" in str(excinfo.value)
    assert "bad token" not in str(excinfo.value)


def test_installation_credentials_are_both_required_and_the_key_stays_out() -> None:
    environ = {"FLAKEPILOT_DATABASE_URL": DSN}
    assert Settings.from_environ(environ).github_installation is None
    material = "super-secret-key-material"
    with pytest.raises(SettingsError) as missing:
        Settings.from_environ({**environ, "FLAKEPILOT_GITHUB_APP_ID": "12345"})
    assert "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY" in str(missing.value)
    assert "12345" not in str(missing.value)
    with pytest.raises(SettingsError) as rejected:
        Settings.from_environ(
            {
                **environ,
                "FLAKEPILOT_GITHUB_APP_ID": "12345",
                "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY": material,
            }
        )
    assert "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY" in str(rejected.value)
    assert material not in str(rejected.value)


def test_every_settings_variable_is_documented_in_env_example() -> None:
    """Each FLAKEPILOT_* name Settings reads must appear in .env.example.

    Regressed in the release-readiness audit: the code-host access verifier's
    FLAKEPILOT_GITHUB_APP_ID and FLAKEPILOT_GITHUB_APP_PRIVATE_KEY were parsed
    and validated by Settings but missing from the documented example, so a
    deployment following it could not configure verification at all.
    """
    repo_root = Path(__file__).resolve().parents[3]
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^FLAKEPILOT_[A-Za-z0-9_]+", example, re.MULTILINE))
    settings_source = (
        repo_root / "src/flakepilot/interface/composition_root/settings.py"
    ).read_text(encoding="utf-8")
    read = set(re.findall(r"FLAKEPILOT_[A-Za-z0-9_]+", settings_source))
    assert read <= documented, sorted(read - documented)


def test_ai_assist_config_is_read_and_together() -> None:
    environ = {
        "FLAKEPILOT_DATABASE_URL": DSN,
        "FLAKEPILOT_AI_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
        "FLAKEPILOT_AI_MODEL": "gemma_3_4b_it",
    }
    settings = Settings.from_environ(environ)
    assert settings.ai_endpoint == environ["FLAKEPILOT_AI_ENDPOINT"]
    assert settings.ai_model == "gemma_3_4b_it"
    assert settings.ai_api_key is None


def test_ai_endpoint_without_a_model_is_a_startup_error() -> None:
    with pytest.raises(SettingsError) as error:
        Settings.from_environ(
            {
                "FLAKEPILOT_DATABASE_URL": DSN,
                "FLAKEPILOT_AI_ENDPOINT": "http://127.0.0.1:9/v1",
            }
        )
    assert "FLAKEPILOT_AI_MODEL" in str(error.value)


def test_ai_model_without_an_endpoint_is_a_startup_error() -> None:
    with pytest.raises(SettingsError) as error:
        Settings.from_environ(
            {"FLAKEPILOT_DATABASE_URL": DSN, "FLAKEPILOT_AI_MODEL": "gemma_3_4b_it"}
        )
    assert "FLAKEPILOT_AI_ENDPOINT" in str(error.value)


def test_ai_endpoint_must_be_http() -> None:
    with pytest.raises(SettingsError):
        Settings.from_environ(
            {
                "FLAKEPILOT_DATABASE_URL": DSN,
                "FLAKEPILOT_AI_ENDPOINT": "ftp://127.0.0.1/x",
                "FLAKEPILOT_AI_MODEL": "gemma_3_4b_it",
            }
        )


def test_the_ai_api_key_never_appears_in_repr_or_str() -> None:
    settings = Settings.from_environ(
        {
            "FLAKEPILOT_DATABASE_URL": DSN,
            "FLAKEPILOT_AI_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
            "FLAKEPILOT_AI_MODEL": "gemma_3_4b_it",
            "FLAKEPILOT_AI_API_KEY": "local-secret-key-9",
        }
    )
    for text in (repr(settings), str(settings)):
        assert "local-secret-key-9" not in text
    assert (
        settings.ai_api_key is not None
        and settings.ai_api_key.reveal() == "local-secret-key-9"
    )


SENTRY_DSN = "https://0123abcdpublic@o42.ingest.example.invalid/4508"


def test_sentry_dsn_is_optional_and_stays_out_of_repr() -> None:
    assert Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN}).sentry_dsn is None
    settings = Settings.from_environ(
        {"FLAKEPILOT_DATABASE_URL": DSN, "FLAKEPILOT_SENTRY_DSN": SENTRY_DSN}
    )
    assert settings.sentry_dsn is not None
    assert settings.sentry_dsn.reveal() == SENTRY_DSN
    for text in (repr(settings), str(settings)):
        assert "0123abcdpublic" not in text


@pytest.mark.parametrize(
    "dsn",
    [
        "http://0123abcdpublic@o42.ingest.example.invalid/4508",
        "https://0123abcdpublic:legacysecret@o42.ingest.example.invalid/4508",
        "https://o42.ingest.example.invalid/4508",
        "https://0123abcdpublic@o42.ingest.example.invalid/",
        "https://0123abcdpublic@o42.ingest.example.invalid/4508?x=1",
        "https://0123abcdpublic@/4508",
    ],
)
def test_invalid_sentry_dsn_is_refused_without_echoing_it(dsn: str) -> None:
    with pytest.raises(SettingsError) as error:
        Settings.from_environ(
            {"FLAKEPILOT_DATABASE_URL": DSN, "FLAKEPILOT_SENTRY_DSN": dsn}
        )
    assert "FLAKEPILOT_SENTRY_DSN" in str(error.value)
    assert "0123abcdpublic" not in str(error.value)
    assert "legacysecret" not in str(error.value)


def test_auth0_issuer_is_off_unless_both_are_set() -> None:
    settings = Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN})
    assert (settings.auth0_domain, settings.auth0_audience) == (None, None)
    settings = Settings.from_environ(
        {
            "FLAKEPILOT_DATABASE_URL": DSN,
            "FLAKEPILOT_AUTH0_DOMAIN": "flakepilot.us.auth0.com",
            "FLAKEPILOT_AUTH0_AUDIENCE": "https://api.flakepilot.example",
        }
    )
    assert settings.auth0_domain == "flakepilot.us.auth0.com"
    assert settings.auth0_audience == "https://api.flakepilot.example"
    for half in ("FLAKEPILOT_AUTH0_DOMAIN", "FLAKEPILOT_AUTH0_AUDIENCE"):
        with pytest.raises(SettingsError, match="must be set together"):
            Settings.from_environ({"FLAKEPILOT_DATABASE_URL": DSN, half: "x.auth0.com"})


@pytest.mark.parametrize(
    "domain",
    [
        "https://tenant.auth0.com",
        "tenant.auth0.com/",
        "Tenant.Auth0.com",
        "tenant.auth0.com:443",
        "localhost",
    ],
)
def test_auth0_domain_must_be_a_bare_host_name(domain: str) -> None:
    with pytest.raises(SettingsError, match="FLAKEPILOT_AUTH0_DOMAIN"):
        Settings.from_environ(
            {
                "FLAKEPILOT_DATABASE_URL": DSN,
                "FLAKEPILOT_AUTH0_DOMAIN": domain,
                "FLAKEPILOT_AUTH0_AUDIENCE": "https://api.flakepilot.example",
            }
        )
