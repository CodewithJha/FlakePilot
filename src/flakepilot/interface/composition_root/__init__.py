"""Composition root: the only place that wires the application together."""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.assist import UnitOfWork as DiagUnitOfWork
from flakepilot.diagnosis.application.store import DiagStore
from flakepilot.diagnosis.infrastructure.store import SqlDiagStore
from flakepilot.identity.application.iam import (
    Iam,
    IamStore,
    IdentityProvider,
    UnitOfWork,
)
from flakepilot.identity.application.verification import CodeHostAccessVerifier
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.github import GitHubIdentityProvider
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root.database import (
    CONNECT_TIMEOUT,
    EngineFactory,
    ReadinessCheck,
    create_engine,
    dispose_engine,
    ping,
    register_database_error_handler,
    transaction,
)
from flakepilot.interface.composition_root.reproduction import Reproductions
from flakepilot.interface.composition_root.run_views import (
    RunViews,
    code_access_verifier,
)
from flakepilot.interface.composition_root.settings import (
    GitHubApp,
    Secret,
    Settings,
    SettingsError,
)
from flakepilot.interface.http import (
    auth,
    errors,
    health,
    ingestion,
    reproductions,
    runs,
)
from flakepilot.interface.http.request_context import (
    RequestContextMiddleware,
    request_id_var,
)
from flakepilot.test_intelligence.application.ingestion import Ingestion
from flakepilot.test_intelligence.application.store import TiStore
from flakepilot.test_intelligence.application.store import (
    UnitOfWork as TiUnitOfWork,
)
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

__all__ = ["GitHubApp", "Secret", "Settings", "SettingsError", "create_app"]

IdentityProviderFactory = Callable[[Settings], IdentityProvider | None]

_HANDLER_NAME = "flakepilot-json"
_logger = logging.getLogger("flakepilot.database")


class JsonFormatter(logging.Formatter):
    """One JSON object per line with a fixed set of fields; request data is never added.

    Optional fields: `client_request_id` (validated inbound X-Request-ID, request log line
    only) and `exception` (server-side tracebacks).
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        client_request_id = getattr(record, "client_request_id", None)
        if client_request_id:
            entry["client_request_id"] = client_request_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str) -> None:
    """Install the JSON handler on the root logger, replacing a previous one."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def _startup_failure(exc: Exception) -> RuntimeError:
    _logger.error("database unavailable at startup: %s", type(exc).__name__)
    return RuntimeError("database unavailable at startup")


def github_identity_provider(settings: Settings) -> IdentityProvider | None:
    """The GitHub sign-in adapter, or None (sign-in disabled) when not configured."""
    if settings.github_app is None or settings.public_origin is None:
        return None
    return GitHubIdentityProvider(
        client_id=settings.github_app.client_id,
        client_secret=settings.github_app.client_secret.reveal(),
        redirect_uri=f"{settings.public_origin}/auth/github/callback",
    )


def iam_unit_of_work(engine: AsyncEngine) -> UnitOfWork:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[IamStore]:
        async with transaction(engine) as session:
            yield SqlIamStore(session)

    return unit_of_work


def diag_unit_of_work(engine: AsyncEngine) -> DiagUnitOfWork:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[DiagStore]:
        async with transaction(engine) as session:
            yield SqlDiagStore(session)

    return unit_of_work


def ti_unit_of_work(engine: AsyncEngine) -> TiUnitOfWork:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        async with transaction(engine) as session:
            yield SqlTiStore(session)

    return unit_of_work


def create_app(
    settings: Settings,
    engine_factory: EngineFactory = create_engine,
    identity_provider_factory: IdentityProviderFactory = github_identity_provider,
    access_verifier: Callable[
        [AsyncEngine, Settings], CodeHostAccessVerifier
    ] = code_access_verifier,
) -> FastAPI:
    """Build the app. No I/O happens until the server runs the lifespan.

    Startup fails fast: the engine is created and checked with one bounded `SELECT 1`;
    on failure the engine is disposed and startup raises (driver details are not chained).
    Migrations are never run here; they are an explicit `alembic upgrade` step (ADR-005).
    Sign-in is enabled only when GitHub and the public origin are configured; otherwise
    the auth endpoints answer 503 `auth_not_configured` (fail closed).
    """
    configure_logging(settings.log_level)
    identity_provider = identity_provider_factory(settings)
    artifacts = (
        LocalFsArtifactStore(settings.artifact_dir)
        if settings.artifact_dir is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            engine = engine_factory(settings.database_url.reveal())
        except Exception as exc:  # noqa: BLE001 - errors may echo the URL
            raise _startup_failure(exc) from None
        try:
            await ping(engine, CONNECT_TIMEOUT)
        except BaseException as exc:
            await dispose_engine(engine)
            if isinstance(exc, Exception):
                raise _startup_failure(exc) from None
            raise
        app.state.readiness_probe = ReadinessCheck(engine)
        app.state.iam = Iam(iam_unit_of_work(engine), identity_provider)
        app.state.assist = Assist(diag_unit_of_work(engine), NullAiAssist())
        app.state.ingestion = (
            Ingestion(ti_unit_of_work(engine), artifacts, JUnitParser())
            if artifacts is not None
            else None
        )
        app.state.reproductions = Reproductions(engine, settings)
        app.state.run_views = RunViews(
            ti_unit_of_work(engine),
            access_verifier(engine, settings),
            app.state.iam.login,
        )
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await dispose_engine(engine)

    # Interactive docs are off: they load third-party scripts from a CDN.
    # The code-first schema stays available at /openapi.json (ADR-007).
    app = FastAPI(
        title="FlakePilot",
        version=version("flakepilot"),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.ready = False
    app.state.auth_config = (
        auth.AuthConfig(settings.public_origin, settings.secure_cookies)
        if identity_provider is not None and settings.public_origin is not None
        else None
    )
    app.add_middleware(RequestContextMiddleware)
    errors.register_error_handlers(app)
    register_database_error_handler(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(ingestion.router)
    app.include_router(reproductions.router)
    app.include_router(runs.router)
    return app
