"""The landing page (`GET /`): the one page a first-time visitor of the public
origin sees before any authentication.

It stays static, unauthed, and database-free: the composition root builds the
app identically for every deployment, and this page needs only the product's
own fixed contract — the sign-in route the `auth` router already mounts and
the shared design system. Everything that follows sign-in has its flows: the
GitHub OAuth round trip lives in `interface.http.auth`, the pages behind a
session in `interface.http.runs`. No script and no request-time data, so the
page renders identically on every deploy and cannot leak anything.

The copy keeps the previous page's exact claims: ingest CI test reports,
classify failures with the deterministic rule set, deterministic
reproduction, verify the fix — and the control plane never executes your
repository's code. The stage diagram lists the investigation workflow's real
stages from `diagnosis/application/investigation.py`; the schematic shows the
run view's shape with explicitly empty cells, not sample data.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from flakepilot.interface.http.ui import HTML_SECURITY_HEADERS, kicker, page

router = APIRouter(tags=["landing"])

_SIGNIN = '<a class="btn" href="/auth/github/login">Sign in with GitHub</a>'

_STAGES = (
    (
        "<span>Ingest</span>",
        "your CI uploads its own reports; receipts stay receipt-shaped",
    ),
    ("<span>Collect</span>", "per-test history and evidence, sorted deterministically"),
    (
        "<span>Hypothesize</span>",
        "an opt-in assist suggests; the rule set still decides",
    ),
    ("<span>Classify</span>", "ti-classification/1 verdicts from stored evidence only"),
    (
        "<span>Reproduce</span>",
        "flaky evidence replayed when the execution host says so",
    ),
)

_CASES = (
    ("tests.classify.test_turnstile", "failed &#8594; passed"),
    ("tests.upload.test_roundtrip", "passed"),
    ("tests.quota.test_cap", "&#8212;"),
)

_BODY = f"""
<section class="hero grid-paper">
{kicker("flaky tests · evidence · a rule set, not vibes")}
<h1>Your CI&#8217;s flaky tests,<br>read like evidence.</h1>
<p class="lede">FlakePilot ingests your CI test reports, reconstructs each
run&#8217;s attempt chains, and classifies failures with the deterministic,
versioned rule set <code>ti-classification/1</code> &mdash; never executing
your repository&#8217;s code inside the control plane.</p>
<div class="hero-actions">
{_SIGNIN}
</div>
</section>

<div class="split">
<section>
<h2>The loop</h2>
<p>Investigations run through the same bounded pipeline every time &mdash; and
every stage you see below runs over <em>stored reports</em>:</p>
<ol class="stage-flow">
{
    "".join(
        f'<li><span class="n">{i + 1}</span>{stage}<span class="why">{why}</span></li>'
        for i, (stage, why) in enumerate(_STAGES)
    )
}
</ol>
<p class="panel-note">Anything advisory (an AI assist, a research source) is
labeled advisory and opt-in; verdicts come from the rule set, the reproduction
claim comes from the host&rsquo;s own answer, and neither is invented by the
UI.</p>
</section>

<section>
<h2>What a run reads like</h2>
<div class="schematic" role="img" aria-label="Schematic of the classified run view with example-shaped but empty data cells">
  <div class="bar"><span class="dot" aria-hidden="true"></span> <strong>Run <code>abc123</code></strong> &#183; ti-classification/1</div>
  <table>
    <tr><th>Test</th><th>Attempts</th><th>Verdict</th></tr>
    {
    "".join(
        f"<tr><td><code>{name}</code></td><td>{chain}</td>"
        '<td><span class="v-sample">&mdash; verdict lands here</span></td></tr>'
        for name, chain in _CASES
    )
}
  </table>
</div>
<p class="panel-note">The classified view is this, minus your data (the cells
fill in from your uploads):
schematically &mdash; attempt chains first, one verdict per test, evidence
listed under every verdict. Nothing here executes your code.</p>
</section>
</div>
"""

_FOOTER = (
    "<span>Read-only control plane. Execution stays with the host, never the web app.</span>"
    "<span>Health probes: <code>/health/live</code> &#183; <code>/health/ready</code></span>"
)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing() -> HTMLResponse:
    return HTMLResponse(
        page(
            title="FlakePilot — flaky tests, read like evidence",
            body=_BODY,
            header_actions=_SIGNIN,
            footer_notes=_FOOTER,
        ),
        status_code=200,
        headers=HTML_SECURITY_HEADERS,
    )
