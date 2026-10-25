"""The pages' own design system: tokens, shell, and the shared stylesheet.

One visual language for every HTML page the app renders — the public landing
page, the session run views, and the operator's offline report — built around
a warm, print-workshop palette, two OFL-licensed typefaces (display + mono)
and a small set of CSS-only graphics (the flake mark, numbered stages, verdict
markers). No runtime JavaScript anywhere: every transition is CSS, honoured
by `prefers-reduced-motion`, and works static.

Security posture of the shared stylesheet: it is served from this origin
(`/ui.css`), cached immutable, carries no secrets, needs no authentication and
sets no cookies. The HTML pages' CSP relaxes exactly two things to make it
work — `style-src` may also load same-origin CSS and `font-src data:` keeps
the base64-embedded OFL fonts inside the policy — while `default-src 'none'`
continues to deny scripts and every else, and framing stays denied.

The tokens below double as the recorded palette decisions for future work:
change a token here, every route follows. Contrast-checked pairs (WCAG 2.2
AA): ink/paper 11.3:1, muted/paper 4.5:1, ink-on-lime 10.2:1, ink-on-mint
8.0:1, ink-on-sand 9.0:1; `--tangerine-border` is the >=3:1 control-boundary
form of the brand tangerine, and error *text* uses `--tomato-ink` (5.4:1) —
`--tomato` stays a graphics colour.
"""

from fastapi import APIRouter
from fastapi.responses import Response

from flakepilot.interface.http.ui_assets import (
    BRICOLAGE_VAR_WOFF2,
    IBM_PLEX_MONO_400_WOFF2,
    IBM_PLEX_MONO_600_WOFF2,
)

router = APIRouter(tags=["ui"])

# Bumped on any stylesheet change; the pages reference `/ui.css?v=<value>` so
# the immutable cache repopulates rather than serving a stale design.
UI_BUILD = "ui-2026-10-26-1"

# The HTML pages' browser-side policy. Diff from the pre-redesign policy:
# `style-src` additionally allows same-origin stylesheets (this directory's
# own /ui.css), `img-src data:` covers the self-contained favicon mark, and
# `font-src data:` covers the embedded typefaces. Scripts, third-party
# origins, framing and HTML-attribute base URLs stay fully denied.
HTML_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline' 'self'; img-src data:; "
        "font-src data:; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
}

_FONTS = (
    "@font-face{font-family:'Bricolage Grotesque';font-style:normal;"
    "font-weight:200 800;font-stretch:100%;font-display:swap;"
    f"src:url(data:font/woff2;base64,{BRICOLAGE_VAR_WOFF2}) format('woff2-variations')}}"
    "@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:400;"
    "font-display:swap;"
    f"src:url(data:font/woff2;base64,{IBM_PLEX_MONO_400_WOFF2}) format('woff2')}}"
    "@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:600;"
    "font-display:swap;"
    f"src:url(data:font/woff2;base64,{IBM_PLEX_MONO_600_WOFF2}) format('woff2')}}"
)

DESIGN_TOKENS_CSS = """
:root{
  --paper:#F7F0DF; --ink:#28352A; --tangerine:#F36B3F; --tangerine-border:#DC4B1D;
  --lime:#D6F25A; --tomato:#E34D3B; --tomato-ink:#B33224; --mint:#B8D4B0;
  --sand:#E6D7B9; --muted:#716E60;
  --font-display:'Bricolage Grotesque',system-ui,sans-serif;
  --font-mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --r-s:6px; --r-m:9px; --r-p:999px;
  --t-fast:140ms; --t-med:220ms; --t-slow:320ms;
  --ease:cubic-bezier(.2,.7,.3,1);
  --sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-6:1.5rem; --sp-8:2rem;
}
"""

PAGE_CSS = (
    DESIGN_TOKENS_CSS
    + """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--font-mono);font-size:15.5px;line-height:1.55;
  min-height:100dvh;display:flex;flex-direction:column}
:focus-visible{outline:2px solid var(--ink);outline-offset:2px;border-radius:2px}
main :focus-visible{outline-color:var(--ink)}
a{color:var(--ink);text-decoration-color:var(--tangerine-border);
  text-decoration-thickness:1.5px;text-underline-offset:3px;
  transition:text-decoration-thickness var(--t-fast) var(--ease)}
a:hover{text-decoration-thickness:3px}
code{font-family:var(--font-mono);font-size:.92em;background:var(--sand);
  color:var(--ink);
  padding:.05em .32em;border-radius:var(--r-s);word-break:break-all;
  font-variant-ligatures:none}
h1,h2,h3{font-family:var(--font-display);font-weight:700;line-height:1.06;
  letter-spacing:-.015em;margin:0 0 var(--sp-3)}
h1{font-size:clamp(1.9rem,4.2vw,2.9rem);max-width:24ch}
h2{font-size:clamp(1.2rem,2.4vw,1.6rem);margin-top:var(--sp-8)}
p{margin:0 0 var(--sp-3);max-width:68ch}
.app-shell{width:min(72rem,100%);margin:0 auto;padding:0 clamp(.9rem,3vw,2rem)}
.app-header{border-bottom:2px solid var(--ink)}
.app-header-grid{display:flex;align-items:center;gap:var(--sp-4);
  padding:var(--sp-4) 0 .9rem;flex-wrap:wrap}
.wordmark{display:inline-flex;align-items:center;gap:.55rem;font-weight:700;
  font-size:1.22rem;letter-spacing:-.01em;text-decoration:none}
.wordmark .flake{width:1.35em;height:1.35em;color:var(--tangerine);
  transition:transform var(--t-med) var(--ease)}
.wordmark:hover .flake{transform:rotate(18deg)}
.wordmark b{font-family:var(--font-display);font-weight:800}
.wordmark i{font-style:normal;font-size:.72rem;color:var(--muted);
  font-weight:400;letter-spacing:.08em;text-transform:uppercase}
.header-actions{margin-left:auto;display:flex;gap:var(--sp-3);align-items:center}
.app-main{flex:1;width:100%;padding:clamp(1.2rem,4vw,2.6rem) 0 var(--sp-8);
  animation:rise var(--t-med) var(--ease) both}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
.app-footer{border-top:2px solid var(--ink);margin-top:var(--sp-8)}
.app-footer-inner{display:flex;flex-wrap:wrap;gap:var(--sp-2) var(--sp-6);
  padding:var(--sp-4) 0 var(--sp-6);color:var(--muted);font-size:.8rem}
.app-footer-inner span{margin:0}
.kicker{display:flex;align-items:center;gap:.55rem;color:var(--muted);
  font-size:.78rem;text-transform:uppercase;letter-spacing:.14em;margin:0 0 var(--sp-3)}
.kicker::before{content:"";width:.85em;height:.85em;background:var(--tangerine);
  clip-path:polygon(0 0,100% 50%,0 100%);flex:none}
.btn{display:inline-flex;align-items:center;gap:.5rem;font:600 .95rem var(--font-mono);
  color:var(--paper);background:var(--ink);border:2px solid var(--ink);
  padding:.6rem 1.05rem;border-radius:var(--r-s);cursor:pointer;
  text-decoration:none;box-shadow:3px 3px 0 var(--tangerine);
  transition:background var(--t-fast) var(--ease),color var(--t-fast) var(--ease),
    box-shadow var(--t-fast) var(--ease),transform var(--t-fast) var(--ease)}
.btn:hover{background:var(--lime);color:var(--ink);
  box-shadow:4px 4px 0 var(--ink);transform:translate(-1px,-1px)}
.btn:active{box-shadow:1px 1px 0 var(--ink);transform:translate(1px,1px)}
.btn-secondary{background:var(--paper);color:var(--ink);
  box-shadow:3px 3px 0 var(--sand)}
.btn-secondary:hover{background:var(--lime);box-shadow:4px 4px 0 var(--ink)}
.hero{padding:clamp(1.4rem,5vw,3.4rem) 0 var(--sp-6)}
.hero h1{font-size:clamp(2.3rem,6.4vw,4.1rem);margin-bottom:0;
  text-wrap:balance}
.hero .lede{font-size:1.02rem;color:var(--ink);margin:var(--sp-4) 0 var(--sp-6)}
.hero-actions{display:flex;flex-wrap:wrap;gap:var(--sp-3);margin-top:var(--sp-2)}
.grid-paper{background-image:repeating-linear-gradient(0deg,transparent 0 23px,
  #28352A12 23px 24px),repeating-linear-gradient(90deg,transparent 0 23px,
  #28352A12 23px 24px)}
.sheet{border:2px dashed var(--tangerine-border);border-radius:var(--r-m);
  padding:var(--sp-4) var(--sp-6);background:#F7F0DFCC}
.stage-flow{list-style:none;margin:0 0 var(--sp-4);padding:0;
  display:grid;gap:.65rem}
.stage-flow li{display:grid;grid-template-columns:2.1rem 7.4rem 1fr;gap:.8rem;
  align-items:baseline;border-left:3px solid var(--sand);padding:.15rem 0 .15rem .8rem;
  transition:border-color var(--t-med) var(--ease)}
.stage-flow li:hover{border-left-color:var(--tangerine)}
.stage-flow .n{font-family:var(--font-display);font-weight:800;font-size:1.35rem;
  color:var(--tangerine-border);font-variant-numeric:tabular-nums}
.stage-flow .stage{font-weight:600}
.stage-flow .why{color:var(--muted);font-size:.86rem;min-width:0}
.stage-flow li{min-width:0}
.data-table{border-collapse:collapse;width:100%;font-size:.88rem;
  font-variant-numeric:tabular-nums}
.data-table th{font:600 .74rem var(--font-mono);text-transform:uppercase;
  letter-spacing:.09em;color:var(--muted);text-align:left;
  border-bottom:2px solid var(--ink);padding:.5rem .55rem;white-space:nowrap}
.data-table td{border-bottom:1px solid var(--sand);padding:.55rem;
  vertical-align:top}
.data-table tbody tr{transition:background var(--t-fast) var(--ease)}
.data-table tbody tr:hover{background:#E6D7B94D}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.data-table{min-width:44rem}
.panel-note{color:var(--muted);font-size:.8rem;max-width:72ch}
.note{color:var(--muted);font-size:.78rem;font-weight:400}
.muted{color:var(--muted)}
.upload-grid th{color:var(--ink);text-transform:none;letter-spacing:0;
  font-size:.84rem;padding:.5rem .55rem;border-bottom:1px solid var(--sand);
  white-space:nowrap;font-weight:400}
.upload-grid td{white-space:pre-wrap}
.context-line{display:flex;flex-wrap:wrap;gap:.4rem .6rem;align-items:baseline;
  color:var(--muted);font-size:.82rem;margin:0 0 var(--sp-4)}
.ctx-chip{background:var(--sand);border-radius:var(--r-p);padding:.14rem .6rem;
  color:var(--ink);font-size:.8rem}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));
  gap:var(--sp-3);margin:var(--sp-4) 0 var(--sp-6)}
.metric{border:2px solid var(--ink);border-radius:var(--r-m);padding:.7rem .8rem;
  background:var(--paper);min-width:0}
.metric b{display:block;font-family:var(--font-display);font-weight:800;
  font-size:clamp(1.5rem,3vw,2.1rem);line-height:1;font-variant-numeric:tabular-nums}
.metric span{display:block;color:var(--muted);font-size:.72rem;
  text-transform:uppercase;letter-spacing:.08em;margin-top:.3rem}
.metric.m-pass b{color:#2E5D3A}
.metric.m-fail b{color:var(--tomato-ink)}
.metric.m-skip b{color:var(--muted)}
.readonly-stamp{display:inline-block;border:2px solid var(--tangerine-border);
  color:var(--ink);border-radius:var(--r-s);padding:.28rem .6rem;
  font:700 .8rem var(--font-display);letter-spacing:.1em;text-transform:uppercase;
  transform:rotate(-2.5deg);white-space:nowrap}
.verdict{display:inline-flex;align-items:center;gap:.45rem;
  border:2px solid var(--ink);border-radius:var(--r-p);padding:.18rem .68rem .18rem .42rem;
  font-weight:600;font-size:.86rem;white-space:nowrap}
.verdict::before{content:"";width:.9em;height:.9em;flex:none}
.v-flaky{border-color:var(--tangerine-border)}
.v-flaky::before{background:var(--tangerine);border-radius:50%}
.v-deterministic_regression{border-color:var(--tomato)}
.v-deterministic_regression::before{background:var(--tomato);border-radius:2px}
.v-infrastructure_failure{border-color:var(--muted)}
.v-infrastructure_failure::before{background:var(--sand);
  border:2px solid var(--muted);width:.78em;height:.78em}
.v-inconclusive{border-style:dashed;border-color:var(--muted)}
.v-inconclusive::before{background:transparent;border:2px solid var(--muted);
  border-radius:50%;width:.74em;height:.74em}
.verdict .note{font-weight:400;color:var(--muted);text-transform:none}
.schematic{border:2px solid var(--ink);border-radius:var(--r-m);
  background:var(--paper);box-shadow:5px 5px 0 var(--mint);
  overflow:hidden;margin:var(--sp-6) 0}
.schematic .bar{display:flex;align-items:center;gap:.5rem;
  border-bottom:2px solid var(--ink);background:var(--sand);padding:.5rem .8rem;
  font-size:.8rem}
.schematic .dot{width:.7em;height:.7em;border-radius:50%;background:var(--tangerine)}
.schematic table{border-collapse:collapse;width:100%;font-size:.82rem}
.schematic th{font:600 .72rem var(--font-mono);text-transform:uppercase;
  letter-spacing:.09em;color:var(--muted);text-align:left;
  border-bottom:2px solid var(--ink);padding:.45rem .7rem}
.schematic td{border-bottom:1px dashed var(--sand);padding:.5rem .7rem;color:var(--muted)}
.schematic .v-sample{border:2px dashed var(--muted);border-radius:var(--r-p);
  padding:.1rem .55rem;color:var(--muted);font-size:.74rem}
.split{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);
  gap:clamp(1rem,4vw,2.4rem);align-items:start}
@media (max-width:860px){.split{grid-template-columns:1fr}}
@media (max-width:900px){.metrics{grid-template-columns:repeat(3,1fr)}}
@media (max-width:560px){.metrics{grid-template-columns:repeat(2,1fr)}
  .stage-flow li{grid-template-columns:1.7rem 1fr;}
  .stage-flow .why{grid-column:2}
  .metric b{font-size:1.4rem}
  h1{font-size:clamp(1.7rem,7.5vw,2.2rem)}}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important;transition:none !important}
}
"""
)

CSS_COLUMN_DOC = DESIGN_TOKENS_CSS + PAGE_CSS + _FONTS


@router.get("/ui.css", include_in_schema=False)
async def stylesheet() -> Response:
    """The shared stylesheet, same-origin and cache-immutable.

    No secrets, no authentication, and no cookies cross this: it exists so the
    three page builders share one visual system instead of duplicating a large
    inline `<style>` per request. `font-src data:` covers the embedded OFL
    fonts; no third-party origin is contacted for any asset.
    """
    return Response(
        content=CSS_COLUMN_DOC,
        media_type="text/css; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            **HTML_SECURITY_HEADERS,
        },
    )


_FLAKE_MARK = (
    '<svg class="flake" viewBox="0 0 32 32" fill="none" aria-hidden="true" '
    'focusable="false"><path d="M16 3v26M6.4 9.5l19.2 13M25.6 9.5l-19.2 13" '
    'stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>'
    '<path d="M16 3l-3.2 4.6h6.4L16 3z" fill="currentColor"/></svg>'
)


def page(
    *,
    title: str,
    body: str,
    header_actions: str = "",
    footer_notes: str = "",
) -> str:
    """One full HTML document in the shared shell.

    `body` is already-built page content (every page builder escapes its own
    data). The shell adds the wordmark header, main landmark and footer; it
    references the shared stylesheet and sets the document lang/metadata.
    """
    crashed_flake = _FLAKE_MARK
    href = f"/ui.css?v={UI_BUILD}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M16 3v26M6.4 9.5l19.2 13M25.6 9.5l-19.2 13' stroke='%23F36B3F' stroke-width='3' stroke-linecap='round'/%3E%3Cpath d='M16 3l-3.2 4.6h6.4L16 3z' fill='%23F36B3F'/%3E%3C/svg%3E">
<link rel="stylesheet" href="{href}">
</head>
<body>
<header class="app-header">
  <div class="app-shell app-header-grid">
    <span class="wordmark">{crashed_flake}<b>FlakePilot</b><i>test evidence, read-only</i></span>
    <span class="header-actions">{header_actions}</span>
  </div>
</header>
<main class="app-shell app-main">
{body}
</main>
<footer class="app-footer">
  <div class="app-shell app-footer-inner">{footer_notes}</div>
</footer>
</body>
</html>
"""


def kicker(text: str) -> str:
    return f'<p class="kicker">{text}</p>'


__all__ = [
    "HTML_SECURITY_HEADERS",
    "PAGE_CSS",
    "UI_BUILD",
    "kicker",
    "page",
    "router",
]
