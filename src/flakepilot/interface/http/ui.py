"""Design tokens for the operator's offline report, and the browser policy
shared by every HTML document the API serves.

The signed-in pages and the public landing page are drawn by the React build
(`shell.py`). This stylesheet is the same palette and type, inlined into the
operator report so that file stays self-contained: no network, no font files,
system faces standing in for Fraunces, Literata and Fragment Mono. It is also
published at `/ui.css` (immutable, no secrets, no authentication).

Contrast-checked pairs (WCAG 2.2 AA) on the paper `#F4EFE6`: ink 15.3:1,
muted 7.9:1, pass 5.7:1, vermilion-ink 6.9:1. Paper on olive 13.7:1, lime on
olive 14.0:1, paper on vermilion-ink 6.9:1. The bright vermilion `#E23D12`
is a large-graphic colour (3.7:1); small text uses vermilion-ink.
"""

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["ui"])

# The HTML documents' browser policy. Scripts, styles and fonts are files on
# this origin. No inline script, no eval, no third-party origin, no framing.
HTML_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "font-src 'self'; img-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
}

DESIGN_TOKENS_CSS = """
:root{
  --paper:#F4EFE6; --paper-2:#E7DCC6; --ink:#1C1915; --muted:#4E483F;
  --olive:#1B261C; --olive-ink:#D5DECF; --vermilion:#E23D12;
  --vermilion-ink:#9C240C; --lime:#E7FF3D; --pass:#1F6B3A;
  --font-display:Fraunces, Georgia, serif;
  --font-body:Literata, Georgia, serif;
  --font-mono:"Fragment Mono", ui-monospace, monospace;
}
"""

PAGE_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--font-body);font-size:16.5px;line-height:1.5}
h1,h2{font-family:var(--font-display);font-weight:620;letter-spacing:-.03em;
  line-height:.98;margin:0 0 .6rem}
h1{font-size:2.4rem;max-width:22ch}
h2{font-size:1.5rem;margin-top:1.8rem}
p{max-width:68ch}
code{font-family:var(--font-mono);font-size:.9em;word-break:break-all}
a{color:var(--vermilion-ink)}
table{border-collapse:collapse;width:100%;margin:0 0 1rem;table-layout:fixed}
th,td{text-align:left;vertical-align:top;padding:.45rem .6rem .45rem 0;
  border-bottom:1px solid var(--paper-2);overflow-wrap:anywhere}
th{font-family:var(--font-mono);font-size:.78rem;font-weight:400;
  letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.muted,.note{color:var(--muted)}
.verdict{display:inline-block;padding:.05rem .3rem;font-weight:700}
.v-flaky{background:var(--lime);color:var(--ink)}
.v-deterministic_regression{color:var(--vermilion-ink)}
.v-infrastructure_failure{color:var(--olive)}
.v-inconclusive{color:var(--vermilion-ink)}
.report-body{border-top:8px solid var(--vermilion)}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important;transition:none !important}
}
"""


@router.get("/ui.css", include_in_schema=False)
async def stylesheet() -> Response:
    """The offline-report stylesheet, same-origin and cache-immutable.

    No secrets, no authentication, and no cookies. The served pages do not
    use it; their CSS is the React build under `/ui/assets/`.
    """
    return Response(
        content=DESIGN_TOKENS_CSS + PAGE_CSS,
        media_type="text/css; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            **HTML_SECURITY_HEADERS,
        },
    )


__all__ = [
    "DESIGN_TOKENS_CSS",
    "HTML_SECURITY_HEADERS",
    "PAGE_CSS",
    "router",
]
