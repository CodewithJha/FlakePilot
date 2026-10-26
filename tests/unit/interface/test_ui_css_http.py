"""The shared design tokens (`/ui.css`) and the React document shell.

`/ui.css` is the operator report's palette, published same-origin with an
immutable cache and no secrets. The HTML pages reference the built
stylesheet and script under `/ui/assets/` instead of inlining either.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from support import FAKE_DSN, send_raw

from flakepilot.interface.composition_root import Secret, Settings, create_app
from flakepilot.interface.http.runs import _index_page, _page


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
    assert "--vermilion:#E23D12" in text and "--ink:#1C1915" in text
    assert "--lime:#E7FF3D" in text
    assert "@font-face" not in text and "data:font" not in text
    assert "<script" not in text
    assert "prefers-reduced-motion" in text


def test_landing_references_the_built_assets() -> None:
    body = _get("/")[2]
    assert 'href="/ui/assets/' in body and body.index('href="/ui/assets/') < body.index(
        ".css"
    )
    assert 'src="/ui/assets/' in body and ".js" in body


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


def test_run_view_references_the_built_assets() -> None:
    report = SimpleNamespace(run=_RunShape(), cases=())
    body = _page(report)  # type: ignore[arg-type]
    assert 'src="/ui/assets/' in body and ".js" in body


def test_runs_index_references_the_built_assets() -> None:
    index = SimpleNamespace(
        org_id=_RunShape.org_id, repo_id=_RunShape.repo_id, entries=()
    )
    body = _index_page(index)  # type: ignore[arg-type]
    assert 'src="/ui/assets/' in body and ".js" in body
