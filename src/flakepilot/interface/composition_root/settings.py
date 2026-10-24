"""Process settings, read once from environment variables (ADR-010). Empty means unset."""

import base64
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from flakepilot.integrations.infrastructure.artifact_store import root_problem
from flakepilot.integrations.infrastructure.browserbase import allowlist_ok
from flakepilot.integrations.infrastructure.elevenlabs import (
    MODELS as ELEVENLABS_MODELS,
)
from flakepilot.integrations.infrastructure.elevenlabs import voice_ok
from flakepilot.integrations.infrastructure.gemma import model_ok as gemma_model_ok
from flakepilot.integrations.infrastructure.installation import (
    app_id_ok,
    private_key_ok,
)
from flakepilot.integrations.infrastructure.sandbox import image_ok

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class SettingsError(ValueError):
    """Invalid configuration. Messages name the variable and never include its value."""


@dataclass(frozen=True)
class Secret:
    """A value kept out of repr, str and logs. Call `reveal()` only at the point of use."""

    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


@dataclass(frozen=True)
class GitHubApp:
    """GitHub App credentials for sign-in (ADR-009). From the environment at bootstrap."""

    client_id: str
    client_secret: Secret


@dataclass(frozen=True)
class GitHubInstallation:
    """App id and private key used only to mint a contents:read installation token."""

    app_id: str
    private_key: Secret


@dataclass(frozen=True)
class Settings:
    database_url: Secret
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    # Sign-in is enabled only when both are set; auth endpoints answer 503 otherwise.
    public_origin: str | None = None
    github_app: GitHubApp | None = None
    secure_cookies: bool = True
    # 32-byte key for the encrypted secret store (ADR-009), base64; not wired yet.
    secret_key: Secret | None = None
    # Artifact storage root (ADR-013). Ingestion answers 503 while it is unset.
    artifact_dir: Path | None = None
    # Leased worker (`... worker`). Empty means these defaults. The API process ignores them.
    worker_poll_seconds: int = 2
    worker_batch_size: int = 10
    worker_lease_seconds: int = 30
    # closed is the only mode the worker runs. evaluation is stored and does not
    # select a container on this process.
    execution_mode: str = "closed"
    execution_runtime: str = "docker"
    execution_image: str | None = None
    execution_wall_seconds: int = 30
    execution_memory_mb: int = 512
    execution_pids: int = 64
    # Parsed so an old environment still loads. Execution does not send this token.
    github_contents_token: Secret | None = None
    # Both unset: execution does not call GitHub. One without the other is refused.
    github_installation: GitHubInstallation | None = None
    # Advisory model endpoint (ADR-017): an operator-run local inference server.
    # Unset: no model contact at all (NullAiAssist posture). Not a secret: it is
    # the operator's own local URL, but never logged either way.
    ai_endpoint: str | None = None
    ai_model: str | None = None
    ai_api_key: Secret | None = None
    # Opt-in error reporting (goal 7). Unset: nothing is sent anywhere.
    sentry_dsn: Secret | None = None
    # Auth0 agent-token issuer (goal 9): both set, or neither (verification off).
    auth0_domain: str | None = None
    auth0_audience: str | None = None
    # Hosted providers (ADR-017), read by the API process and operator commands
    # only. Each is off, with no network contact, unless its key is set; a pair
    # with one half set refuses startup.
    gemini_api_key: Secret | None = None
    gemini_model: str | None = None
    serpapi_api_key: Secret | None = None
    browserbase_api_key: Secret | None = None
    browser_allowlist: tuple[str, ...] = ()
    elevenlabs_api_key: Secret | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_flash_v2_5"
    tabpfn_api_key: Secret | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "Settings":
        database_url = environ.get("FLAKEPILOT_DATABASE_URL")
        if not database_url:
            raise SettingsError("FLAKEPILOT_DATABASE_URL is required")
        _validate_database_url(database_url)

        log_level = (environ.get("FLAKEPILOT_LOG_LEVEL") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise SettingsError(
                f"FLAKEPILOT_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}"
            )

        host = environ.get("FLAKEPILOT_HOST") or "127.0.0.1"
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise SettingsError("FLAKEPILOT_HOST must be an IP address") from None

        port = environ.get("FLAKEPILOT_PORT") or "8000"
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise SettingsError("FLAKEPILOT_PORT must be an integer from 1 to 65535")

        insecure = (environ.get("FLAKEPILOT_ALLOW_INSECURE_COOKIES") or "false").lower()
        if insecure not in ("true", "false"):
            raise SettingsError(
                "FLAKEPILOT_ALLOW_INSECURE_COOKIES must be true or false"
            )
        secure_cookies = insecure == "false"

        public_origin = environ.get("FLAKEPILOT_PUBLIC_ORIGIN") or None
        if public_origin is not None:
            public_origin = _origin(public_origin, secure_cookies)

        client_id = environ.get("FLAKEPILOT_GITHUB_CLIENT_ID") or None
        client_secret = environ.get("FLAKEPILOT_GITHUB_CLIENT_SECRET") or None
        github_app = None
        if client_id or client_secret:
            for name, value in (
                ("FLAKEPILOT_GITHUB_CLIENT_ID", client_id),
                ("FLAKEPILOT_GITHUB_CLIENT_SECRET", client_secret),
                ("FLAKEPILOT_PUBLIC_ORIGIN", public_origin),
            ):
                if not value:
                    raise SettingsError(f"{name} is required for GitHub sign-in")
            assert client_id and client_secret
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", client_id):
                raise SettingsError(
                    "FLAKEPILOT_GITHUB_CLIENT_ID is not a valid client ID"
                )
            github_app = GitHubApp(client_id, Secret(client_secret))

        secret_key = environ.get("FLAKEPILOT_SECRET_KEY") or None
        if secret_key is not None and len(_decode_key(secret_key)) != 32:
            raise SettingsError(
                "FLAKEPILOT_SECRET_KEY must be 32 bytes, base64-encoded"
            )

        artifact_dir = environ.get("FLAKEPILOT_ARTIFACT_DIR") or None
        auth0_domain, auth0_audience = _auth0(environ)
        gemini_api_key, gemini_model = _gemini(environ)
        if gemini_api_key is not None and environ.get("FLAKEPILOT_AI_ENDPOINT"):
            raise SettingsError(
                "FLAKEPILOT_AI_ENDPOINT and FLAKEPILOT_GEMINI_API_KEY both configure "
                "the advisory model; set at most one"
            )
        browserbase_api_key, browser_allowlist = _browserbase(environ)
        elevenlabs_api_key, elevenlabs_voice_id, elevenlabs_model = _elevenlabs(environ)
        return cls(
            Secret(database_url),
            log_level=log_level,
            host=host,
            port=int(port),
            public_origin=public_origin,
            github_app=github_app,
            secure_cookies=secure_cookies,
            secret_key=Secret(secret_key) if secret_key else None,
            artifact_dir=_artifact_dir(artifact_dir) if artifact_dir else None,
            worker_poll_seconds=_bounded(
                environ, "FLAKEPILOT_WORKER_POLL_SECONDS", 2, 1, 300
            ),
            worker_batch_size=_bounded(
                environ, "FLAKEPILOT_WORKER_BATCH_SIZE", 10, 1, 100
            ),
            worker_lease_seconds=_bounded(
                environ, "FLAKEPILOT_WORKER_LEASE_SECONDS", 30, 5, 3600
            ),
            execution_mode=_execution_mode(environ),
            execution_runtime=_execution_runtime(environ),
            execution_image=_execution_image(environ),
            execution_wall_seconds=_bounded(
                environ, "FLAKEPILOT_EXECUTION_WALL_SECONDS", 30, 5, 120
            ),
            execution_memory_mb=_bounded(
                environ, "FLAKEPILOT_EXECUTION_MEMORY_MB", 512, 64, 2048
            ),
            execution_pids=_bounded(environ, "FLAKEPILOT_EXECUTION_PIDS", 64, 8, 256),
            github_contents_token=_contents_token(environ),
            github_installation=_installation(environ),
            ai_endpoint=_ai_endpoint(environ),
            ai_model=_ai_model(environ),
            ai_api_key=_ai_api_key(environ),
            sentry_dsn=_sentry_dsn(environ),
            auth0_domain=auth0_domain,
            auth0_audience=auth0_audience,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            serpapi_api_key=_provider_key(environ, "FLAKEPILOT_SERPAPI_API_KEY"),
            browserbase_api_key=browserbase_api_key,
            browser_allowlist=browser_allowlist,
            elevenlabs_api_key=elevenlabs_api_key,
            elevenlabs_voice_id=elevenlabs_voice_id,
            elevenlabs_model=elevenlabs_model,
            tabpfn_api_key=_provider_key(environ, "FLAKEPILOT_TABPFN_API_KEY"),
        )


def _bounded(
    environ: Mapping[str, str], name: str, default: int, low: int, high: int
) -> int:
    raw = environ.get(name) or str(default)
    if not re.fullmatch(r"[0-9]{1,4}", raw) or not low <= int(raw) <= high:
        raise SettingsError(f"{name} must be an integer from {low} to {high}")
    return int(raw)


def _execution_mode(environ: Mapping[str, str]) -> str:
    mode = environ.get("FLAKEPILOT_EXECUTION_MODE") or "closed"
    if mode not in ("closed", "evaluation"):
        raise SettingsError("FLAKEPILOT_EXECUTION_MODE must be closed or evaluation")
    return mode


def _execution_runtime(environ: Mapping[str, str]) -> str:
    runtime = environ.get("FLAKEPILOT_EXECUTION_RUNTIME") or "docker"
    if runtime not in ("docker", "podman"):
        raise SettingsError("FLAKEPILOT_EXECUTION_RUNTIME must be docker or podman")
    return runtime


def _execution_image(environ: Mapping[str, str]) -> str | None:
    image = environ.get("FLAKEPILOT_EXECUTION_IMAGE") or None
    mode = environ.get("FLAKEPILOT_EXECUTION_MODE") or "closed"
    if image is not None and not image_ok(image):
        raise SettingsError(
            "FLAKEPILOT_EXECUTION_IMAGE must be a digest-pinned reference"
        )
    if mode == "evaluation" and image is None:
        raise SettingsError("FLAKEPILOT_EXECUTION_IMAGE is required for evaluation")
    return image


def _contents_token(environ: Mapping[str, str]) -> Secret | None:
    token = environ.get("FLAKEPILOT_GITHUB_CONTENTS_TOKEN") or None
    if token is None:
        return None
    if len(token) > 500 or any(character.isspace() for character in token):
        raise SettingsError("FLAKEPILOT_GITHUB_CONTENTS_TOKEN is not a token")
    return Secret(token)


def _installation(environ: Mapping[str, str]) -> GitHubInstallation | None:
    app_id = environ.get("FLAKEPILOT_GITHUB_APP_ID") or None
    pem = environ.get("FLAKEPILOT_GITHUB_APP_PRIVATE_KEY") or None
    if app_id is None and pem is None:
        return None
    if not app_id or not pem or not app_id_ok(app_id) or not private_key_ok(pem):
        raise SettingsError(
            "FLAKEPILOT_GITHUB_APP_ID and FLAKEPILOT_GITHUB_APP_PRIVATE_KEY "
            "must both be set to an app id and an unencrypted RSA private key"
        )
    return GitHubInstallation(app_id, Secret(pem))


def _ai_endpoint(environ: Mapping[str, str]) -> str | None:
    endpoint = environ.get("FLAKEPILOT_AI_ENDPOINT") or None
    if endpoint is None:
        return None
    if not endpoint.startswith(("http://", "https://")):
        raise SettingsError("FLAKEPILOT_AI_ENDPOINT must be an http(s) URL")
    if not 1 <= len(endpoint) <= 500:
        raise SettingsError("FLAKEPILOT_AI_ENDPOINT must be a URL of 1-500 characters")
    return endpoint


def _ai_model(environ: Mapping[str, str]) -> str | None:
    endpoint = environ.get("FLAKEPILOT_AI_ENDPOINT") or None
    model = environ.get("FLAKEPILOT_AI_MODEL") or None
    if model is None and endpoint is None:
        return None
    if endpoint is not None and model is None:
        raise SettingsError(
            "FLAKEPILOT_AI_MODEL must be set together with FLAKEPILOT_AI_ENDPOINT "
            "(the model identity is recorded on every response)"
        )
    if model is not None and endpoint is None:
        raise SettingsError(
            "FLAKEPILOT_AI_MODEL without FLAKEPILOT_AI_ENDPOINT configures nothing"
        )
    if model is None:  # every None case is refused by the two checks above
        return None
    if len(model) > 80:
        raise SettingsError("FLAKEPILOT_AI_MODEL must be a model identifier of <= 80")
    return model


def _ai_api_key(environ: Mapping[str, str]) -> Secret | None:
    key = environ.get("FLAKEPILOT_AI_API_KEY") or None
    if key is None:
        return None
    if len(key) > 500 or any(character.isspace() for character in key):
        raise SettingsError("FLAKEPILOT_AI_API_KEY is not a key")
    return Secret(key)


def _sentry_dsn(environ: Mapping[str, str]) -> Secret | None:
    """https://<public key>@<host>[/<prefix>]/<project id>; no legacy secret key."""
    dsn = environ.get("FLAKEPILOT_SENTRY_DSN") or None
    if dsn is None:
        return None
    try:
        parts = urlsplit(dsn)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise SettingsError("FLAKEPILOT_SENTRY_DSN is not a valid DSN") from None
    if (
        len(dsn) > 300
        or parts.scheme != "https"
        or not parts.hostname
        or not re.fullmatch(r"[A-Za-z0-9]{1,64}", parts.username or "")
        or parts.password is not None
        or not re.fullmatch(r"(/[A-Za-z0-9._-]+)*/[0-9]{1,20}", parts.path)
        or parts.query
        or parts.fragment
    ):
        raise SettingsError(
            "FLAKEPILOT_SENTRY_DSN must be https://<public key>@<host>/<project id>"
        )
    return Secret(dsn)


_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)


def _auth0(environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    domain = environ.get("FLAKEPILOT_AUTH0_DOMAIN") or None
    audience = environ.get("FLAKEPILOT_AUTH0_AUDIENCE") or None
    if domain is None and audience is None:
        return None, None
    if domain is None or audience is None:
        raise SettingsError(
            "FLAKEPILOT_AUTH0_DOMAIN and FLAKEPILOT_AUTH0_AUDIENCE must be set together"
        )
    if not _HOSTNAME.fullmatch(domain):
        raise SettingsError(
            "FLAKEPILOT_AUTH0_DOMAIN must be a lowercase host name, e.g. tenant.auth0.com"
        )
    if len(audience) > 200 or not audience.isprintable() or " " in audience:
        raise SettingsError("FLAKEPILOT_AUTH0_AUDIENCE must be an API identifier")
    return domain, audience


def _provider_key(environ: Mapping[str, str], name: str) -> Secret | None:
    key = environ.get(name) or None
    if key is None:
        return None
    if not re.fullmatch(r"[\x21-\x7e]{1,200}", key):
        raise SettingsError(f"{name} must be 1-200 printable characters, no spaces")
    return Secret(key)


def _together(environ: Mapping[str, str], *names: str) -> bool:
    """True when all are set, False when none is; anything else is refused."""
    present = [bool(environ.get(name)) for name in names]
    if any(present) and not all(present):
        raise SettingsError(f"{' and '.join(names)} must be set together")
    return all(present)


def _gemini(environ: Mapping[str, str]) -> tuple[Secret | None, str | None]:
    names = ("FLAKEPILOT_GEMINI_API_KEY", "FLAKEPILOT_GEMINI_MODEL")
    if not _together(environ, *names):
        return None, None
    model = environ[names[1]]
    if not gemma_model_ok(model):
        raise SettingsError(
            "FLAKEPILOT_GEMINI_MODEL must be a Gemini API Gemma model name, "
            "e.g. gemma-4-31b-it"
        )
    return _provider_key(environ, names[0]), model


def _browserbase(environ: Mapping[str, str]) -> tuple[Secret | None, tuple[str, ...]]:
    names = ("FLAKEPILOT_BROWSERBASE_API_KEY", "FLAKEPILOT_BROWSER_ALLOWLIST")
    if not _together(environ, *names):
        return None, ()
    hosts = tuple(sorted({h.strip() for h in environ[names[1]].split(",")} - {""}))
    if not allowlist_ok(hosts):
        raise SettingsError(
            "FLAKEPILOT_BROWSER_ALLOWLIST must be 1-50 comma-separated public "
            "lowercase host names (no IPs, ports, schemes or internal suffixes)"
        )
    return _provider_key(environ, names[0]), hosts


def _elevenlabs(
    environ: Mapping[str, str],
) -> tuple[Secret | None, str | None, str]:
    names = ("FLAKEPILOT_ELEVENLABS_API_KEY", "FLAKEPILOT_ELEVENLABS_VOICE_ID")
    model = environ.get("FLAKEPILOT_ELEVENLABS_MODEL") or "eleven_flash_v2_5"
    if not _together(environ, *names):
        if environ.get("FLAKEPILOT_ELEVENLABS_MODEL"):
            raise SettingsError(
                "FLAKEPILOT_ELEVENLABS_MODEL without FLAKEPILOT_ELEVENLABS_API_KEY "
                "configures nothing"
            )
        return None, None, model
    if not voice_ok(environ[names[1]]):
        raise SettingsError("FLAKEPILOT_ELEVENLABS_VOICE_ID is not a voice ID")
    if model not in ELEVENLABS_MODELS:
        raise SettingsError(
            f"FLAKEPILOT_ELEVENLABS_MODEL must be one of {sorted(ELEVENLABS_MODELS)}"
        )
    return _provider_key(environ, names[0]), environ[names[1]], model


def _artifact_dir(value: str) -> Path:
    path = Path(value)
    problem = root_problem(path)
    if problem is not None:
        raise SettingsError(f"FLAKEPILOT_ARTIFACT_DIR {problem}")
    return path


def _origin(value: str, secure_cookies: bool) -> str:
    """Normalise to scheme://host[:port] as browsers send it in `Origin` (lowercase
    host, default port omitted); nothing else is allowed."""
    name = "FLAKEPILOT_PUBLIC_ORIGIN"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise SettingsError(f"{name} is not a valid origin") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SettingsError(f"{name} must be an http(s) origin")
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
    ):
        raise SettingsError(f"{name} must be only scheme, host and port")
    if parts.scheme == "http" and secure_cookies:
        raise SettingsError(
            f"{name} must use https unless FLAKEPILOT_ALLOW_INSECURE_COOKIES=true"
        )
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default else ""
    return f"{parts.scheme}://{host}{suffix}"


def _decode_key(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise SettingsError("FLAKEPILOT_SECRET_KEY is not valid base64") from None


def _validate_database_url(url: str) -> None:
    """Structural checks only; errors never include any part of the URL."""
    name = "FLAKEPILOT_DATABASE_URL"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise SettingsError(f"{name} is not a valid URL") from None
    if parts.scheme not in ("postgresql", "postgres"):
        raise SettingsError(f"{name} must use the postgresql:// scheme")
    if not parts.hostname:
        raise SettingsError(f"{name} must include a host")
    if not parts.path.strip("/") or "/" in parts.path.strip("/"):
        raise SettingsError(f"{name} must include a database name")
