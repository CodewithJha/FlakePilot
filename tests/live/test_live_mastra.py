"""The Mastra sidecar against a locally served FlakePilot, with real Auth0, Gemma and
SerpApi (goals 2, 9, 1, 8). Owner-run only.

Skipped unless every variable below is set and `sidecars/mastra` has been
installed (`npm ci`), so the default suite and CI contact nothing. A skip is
not verification. What happens:

1. A throwaway migrated database gets an org with one admin, a registered
   repository, one stored flaky run, the org AI policy `evidence_only`, and a
   binding for the live Auth0 client's subject.
2. The real `create_app` is served by uvicorn on a free loopback port, with
   hosted Gemma and SerpApi configured from the `FLAKEPILOT_LIVE_*` keys. The
   code-host standing is a `verified_read` stand-in (no GitHub App exists).
3. `node src/main.ts` runs the Mastra workflow once: it obtains its own token
   from the tenant and calls the agent route.

Budget guard: each provider's transport is wrapped in this process to allow
at most two attempts (Gemini and SerpApi each), and to record attempt status
codes only. Nothing records a key, token, prompt, or answer text.

    FLAKEPILOT_TEST_DATABASE_URL=... FLAKEPILOT_AUTH0_DOMAIN=... \\
    FLAKEPILOT_AUTH0_AUDIENCE=... FLAKEPILOT_LIVE_AUTH0_CLIENT_ID=... \\
    FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET=... FLAKEPILOT_LIVE_GEMINI_API_KEY=... \\
    FLAKEPILOT_LIVE_GEMMA_MODEL=... FLAKEPILOT_LIVE_SERPAPI_API_KEY=... \\
        uv run pytest tests/live -k mastra -s
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, Seeded, seed
from sqlalchemy import text
from support import throwaway_database

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.store import AiPolicy
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.identity.application.iam import Iam, SessionPrincipal
from flakepilot.integrations.infrastructure import gemma, hosted_http, serpapi
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.interface.composition_root import (
    Secret,
    Settings,
    create_app,
    diag_unit_of_work,
    iam_unit_of_work,
)
from flakepilot.interface.composition_root.agent_binding_commands import bind
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import Channel, Origin

SIDECAR = Path(__file__).resolve().parents[2] / "sidecars" / "mastra"
ENV = {
    name: os.environ.get(name, "")
    for name in (
        "FLAKEPILOT_TEST_DATABASE_URL",
        "FLAKEPILOT_AUTH0_DOMAIN",
        "FLAKEPILOT_AUTH0_AUDIENCE",
        "FLAKEPILOT_LIVE_AUTH0_CLIENT_ID",
        "FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET",
        "FLAKEPILOT_LIVE_GEMINI_API_KEY",
        "FLAKEPILOT_LIVE_GEMMA_MODEL",
        "FLAKEPILOT_LIVE_SERPAPI_API_KEY",
    )
}
MAX_ATTEMPTS = 2


class Budget:
    """Per-provider attempt counter and status log around the real transport."""

    def __init__(self) -> None:
        self.statuses: dict[str, list[int | str]] = {"gemini": [], "serpapi": []}

    def wrap(self, provider: str, real: Callable[..., Any]) -> Callable[..., Any]:
        async def exchange(request: urllib.request.Request, **kwargs: Any) -> Any:
            transport = kwargs.pop("transport")

            def counted(*args: Any) -> tuple[int, bytes]:
                if len(self.statuses[provider]) >= MAX_ATTEMPTS:
                    raise hosted_http.HostedError("budget_exhausted")
                try:
                    status, body = transport(*args)
                except hosted_http.HostedError as error:
                    self.statuses[provider].append(error.reason)
                    raise
                self.statuses[provider].append(status)
                return status, body

            return await real(request, transport=counted, **kwargs)

        return exchange


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def _prepare(url: str, settings: Settings) -> Seeded:
    engine = create_engine(url)
    try:
        seeded = await seed(engine)
        iam = Iam(iam_unit_of_work(engine), None)
        admin = await iam.org_access(SessionPrincipal(seeded.admin_id), seeded.org_id)
        await Assist(diag_unit_of_work(engine), NullAiAssist()).set_policy(
            admin,
            org_id=seeded.org_id,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=Origin(Channel.CLI, "live-mastra"),
        )
        await bind(
            settings,
            org_id=seeded.org_id,
            github_id=ADMIN_GITHUB_ID,
            repo_id=seeded.repo_id,
            subject=f"{ENV['FLAKEPILOT_LIVE_AUTH0_CLIENT_ID']}@clients",
            agent_name="mastra_sidecar",
            scopes=["repo.view", "repo.investigate"],
            ttl=timedelta(minutes=15),
        )
        return seeded
    finally:
        await engine.dispose()


async def _provenance(url: str, org_id: str) -> list[Any]:
    engine = create_engine(url)
    try:
        async with transaction(engine) as session:
            return list(
                await session.execute(
                    text(
                        "SELECT name, actor_id, after, reason, "
                        "payload->>'via_agent' AS via, payload->>'provider' AS provider "
                        "FROM diag.outbox WHERE org_id = :org ORDER BY seq"
                    ),
                    {"org": org_id},
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
def served() -> Iterator[tuple[str, Settings]]:
    with throwaway_database(migrate=True) as url:
        settings = Settings(
            Secret(url),
            auth0_domain=ENV["FLAKEPILOT_AUTH0_DOMAIN"],
            auth0_audience=ENV["FLAKEPILOT_AUTH0_AUDIENCE"],
            gemini_api_key=Secret(ENV["FLAKEPILOT_LIVE_GEMINI_API_KEY"]),
            gemini_model=ENV["FLAKEPILOT_LIVE_GEMMA_MODEL"],
            serpapi_api_key=Secret(ENV["FLAKEPILOT_LIVE_SERPAPI_API_KEY"]),
        )
        yield url, settings


@pytest.mark.skipif(
    not all(ENV.values())
    or shutil.which("node") is None
    or not (SIDECAR / "node_modules" / "@mastra" / "core").is_dir(),
    reason="live variables, node, or the installed sidecar are missing",
)
def test_live_mastra_workflow_investigates_through_the_agent_route(
    served: tuple[str, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, settings = served
    budget = Budget()
    monkeypatch.setattr(
        f"{gemma.__name__}.exchange", budget.wrap("gemini", hosted_http.exchange)
    )
    monkeypatch.setattr(
        f"{serpapi.__name__}.exchange", budget.wrap("serpapi", hosted_http.exchange)
    )
    seeded = asyncio.run(_prepare(url, settings))

    port = _free_port()
    app = create_app(settings, access_verifier=lambda _e, _s: ReadVerifier())
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_config=None, access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert server.started, "the API did not start"
    try:
        started = time.monotonic()
        completed = subprocess.run(
            ["node", "src/main.ts", "--org", seeded.org_id, "--run", seeded.run_id],
            cwd=SIDECAR,
            check=False,
            env={
                "PATH": os.environ["PATH"],
                "HOME": os.environ.get("HOME", "/tmp"),
                "FLAKEPILOT_BASE_URL": f"http://127.0.0.1:{port}",
                "FLAKEPILOT_AUTH0_DOMAIN": ENV["FLAKEPILOT_AUTH0_DOMAIN"],
                "FLAKEPILOT_AUTH0_AUDIENCE": ENV["FLAKEPILOT_AUTH0_AUDIENCE"],
                "FLAKEPILOT_AGENT_AUTH0_CLIENT_ID": ENV[
                    "FLAKEPILOT_LIVE_AUTH0_CLIENT_ID"
                ],
                "FLAKEPILOT_AGENT_AUTH0_CLIENT_SECRET": ENV[
                    "FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET"
                ],
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = time.monotonic() - started
    finally:
        server.should_exit = True
        thread.join(timeout=20)

    secret = ENV["FLAKEPILOT_LIVE_AUTH0_CLIENT_SECRET"]
    assert secret not in completed.stdout and secret not in completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    rows = asyncio.run(_provenance(url, seeded.org_id))
    print(
        "\nLIVE MASTRA RUN:",
        json.dumps(
            {
                "exit": completed.returncode,
                "seconds": round(elapsed, 1),
                "ok": outcome.get("ok"),
                "error": outcome.get("error"),
                "stages": (outcome.get("report") or {}).get("stages"),
                "hypothesis": (outcome.get("report") or {}).get("hypothesis"),
                "research": (outcome.get("report") or {}).get("research"),
                "headline": (outcome.get("report") or {}).get("headline"),
                "provider_attempt_statuses": budget.statuses,
                "diag_events": [
                    [r.name, r.after, r.reason, r.via, r.provider] for r in rows
                ],
            },
            indent=2,
        ),
    )
    assert len(budget.statuses["gemini"]) <= MAX_ATTEMPTS
    assert len(budget.statuses["serpapi"]) <= MAX_ATTEMPTS
    assert completed.returncode == 0, outcome
    report = outcome["report"]
    assert report["authority"] == "flakepilot" and report["advisory"] is True
    assert report["run_id"] == seeded.run_id
    investigation = [r for r in rows if r.name == "diag.investigation.recorded"]
    assert [(str(r.actor_id), r.via) for r in investigation] == [
        (seeded.admin_id, "mastra_sidecar")
    ]
