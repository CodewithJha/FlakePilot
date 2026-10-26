"""The React document shell served for the public page and the signed-in views.

The built files live next to this module (`webdist/`) and are what the image
ships. Python still chooses the document: it authorizes the request, then
splices a JSON boot payload into the shell. The payload is encoded so a stored
string cannot close the script element or rewrite the placeholders. The
browser draws it; the script, stylesheet and fonts are same-origin files.
"""

import json
import mimetypes
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

_WEB = Path(__file__).resolve().parent / "webdist"
_TOKENS = ("__FLAKEPILOT_TITLE__", "__FLAKEPILOT_BOOT__", "__FLAKEPILOT_NOSCRIPT__")


class HashedAssets(StaticFiles):
    """Hashed build files can be cached immutably; the HTML documents cannot."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def asset_mount() -> HashedAssets:
    if not _WEB.is_dir():
        raise RuntimeError(_missing())
    return HashedAssets(directory=_WEB)


@lru_cache(maxsize=1)
def _template() -> str:
    path = _WEB / "index.html"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(_missing()) from exc
    missing = [token for token in _TOKENS if token not in text]
    if missing:
        raise RuntimeError(f"web UI template is missing {missing[0]}")
    return text


def _missing() -> str:
    return (
        "The web UI build is missing. From a checkout run: "
        "npm ci --prefix web && npm run build --prefix web"
    )


def _json_for_html(boot: dict[str, Any]) -> str:
    payload = json.dumps(boot, ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return payload.replace("__FLAKEPILOT", "\\u005f\\u005fFLAKEPILOT")


def document(*, title: str, boot: dict[str, Any], noscript: str) -> str:
    """One HTML document: fixed title, boot JSON, and a no-JavaScript note."""
    text = _template()
    text = text.replace("__FLAKEPILOT_TITLE__", title)
    text = text.replace("__FLAKEPILOT_BOOT__", _json_for_html(boot))
    return text.replace("__FLAKEPILOT_NOSCRIPT__", noscript)


__all__ = ["asset_mount", "document"]
