"""The shared design system: the `/ui.css` stylesheet route and the page shell.

Serving one stylesheet from this origin (immutable cache, no secrets, no
auth) is what lets the three HTML page builders share a single visual system
without an inline `<style>` copy per response or any third-party font host.
These tests pin the route's contract and that every HTML page references it.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from support import FAKE_DSN, send_raw

from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.http.runs import _index_page, _page
from flakepilot.interface.http.ui import UI_BUILD


def _get(path: str) -> tuple[int, dict[str, str], str]:
    app = create_app(Settings(Secret(FAKE_DSN)))

    async def call() -> tuple[int, list[tuple[str, str]], bytes]:
        status, headers, body = await send_raw(app, "GET", path)
        return status, [(k.lower(), v) for k, v in headers], body

    status, headers, content = asyncio.run(call())
    return status, dict(headers), content.decode()


def test_ui_css_is_public_cached_and_carries_the_system() -> None:
    status, headers, text = _get("/ui.css")
    assert status == 200
    assert headers["content-type"].startswith("text/css")
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
    # The design tokens and the self-hosted OFL fonts are all in there…
    assert "--tangerine:#F36B3F" in text and "--ink:#28352A" in text
    assert text.count("@font-face") == 3  # Bricolage variable + Plex Mono 400/600
    assert "data:font/woff2;base64" in text
    # …and it is static decoration: no script, honouring reduced motion.
    assert "<script" not in text
    assert "prefers-reduced-motion" in text


def test_landing_references_the_shared_stylesheet() -> None:
    assert f'href="/ui.css?v={UI_BUILD}"' in _get("/")[2]


class _RunShape:
    """The fields the run-view builder reads; values carry no markup."""

    org_id = "o" * 24
    repo_id = "p" * 24
    run_id = "r" * 24
    commit_sha = "c" * 40
    environment = "env"
    source = "ci"
    revision_status = "unchecked"
    received_at: str | None = None
    size_bytes = 0
    artifact_key: str | None = None
    metadata: Any = None
    summary = SimpleNamespace(
        tests=0, attempts=0, passed=0, failed=0, errored=0, skipped=0
    )


def test_run_view_references_the_shared_stylesheet() -> None:
    report = SimpleNamespace(run=_RunShape(), cases=())
    assert f'href="/ui.css?v={UI_BUILD}"' in _page(report)  # type: ignore[arg-type]


def test_runs_index_references_the_shared_stylesheet() -> None:
    index = SimpleNamespace(
        org_id=_RunShape.org_id, repo_id=_RunShape.repo_id, entries=()
    )
    assert f'href="/ui.css?v={UI_BUILD}"' in _index_page(index)  # type: ignore[arg-type]
