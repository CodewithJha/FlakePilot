"""The landing page (`GET /`): static, unauthed, database-free, and the only
page a visitor of the public origin reaches before any authentication. It must
always exist (any deployment, any settings), render with the same no-script /
no-frame security headers as the other pages, and point at the sign-in route
the auth router really mounts."""

import asyncio

from support import FAKE_DSN, send_raw

from flakepilot.interface.composition_root import Secret, Settings, create_app


async def get(path: str) -> tuple[int, list[tuple[str, str]], str]:
    app = create_app(Settings(Secret(FAKE_DSN)))
    status, headers, content = await send_raw(app, "GET", path)
    return status, headers, content.decode()


def test_landing_page_is_public_static_and_hardened() -> None:
    status, headers, body = asyncio.run(get("/"))
    assert status == 200
    assert "text/html" in dict(headers)["content-type"]
    csp = dict(headers)["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert dict(headers)["x-frame-options"] == "DENY"
    assert b"<script" not in body.lower().encode()


def test_landing_page_links_to_the_real_sign_in_route() -> None:
    status, _, body = asyncio.run(get("/"))
    assert status == 200
    assert 'href="/auth/github/login"' in body
    assert "FlakePilot" in body


def test_landing_page_works_with_public_origin_too() -> None:
    """Deployment-config independent (never conditioned on settings)."""
    app = create_app(
        Settings(Secret(FAKE_DSN), public_origin="https://app.flakepilot.tech")
    )

    async def body() -> int:
        status, _, _ = await send_raw(app, "GET", "/")
        return status

    assert asyncio.run(body()) == 200
