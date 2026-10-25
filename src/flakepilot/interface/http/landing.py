"""The landing page (`GET /`): the one page a first-time visitor of the public
origin sees before any authentication.

It is deliberately static, unauthed, and database-free: the composition root
builds the app identically for every deployment, and this page only needs the
product's own fixed contract — the sign-in route the `auth` router already
mounts — plus the same security headers every other page carries. Everything
that follows sign-in already has its flows: the GitHub OAuth round trip lives
in `interface.http.auth`, and the pages behind a session live in
`interface.http.runs` and friends. No script and no external asset loads
(fixed by `HTML_SECURITY_HEADERS`), so the page cannot introduce a CSP
failure by pointing at resources that do not exist.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from flakepilot.interface.http.runs import HTML_SECURITY_HEADERS

router = APIRouter(tags=["landing"])


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlakePilot</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 4rem auto;
       padding: 0 1.5rem; color: #16202c; line-height: 1.55; }
h1 { font-size: 1.9rem; margin-bottom: .3rem; }
p.tagline { color: #5b6b7c; margin-top: 0; }
a.signin { display: inline-block; background: #16202c; color: #fff;
           padding: .6rem 1.1rem; border-radius: .4rem; text-decoration: none; }
a.signin:hover { background: #34455a; }
p.note { font-size: .92rem; color: #5b6b7c; border-top: 1px solid #e4e9ef;
         padding-top: 1rem; margin-top: 2rem; }
code { background: #f2f5f8; padding: .08rem .35rem; border-radius: .25rem; }
</style>
</head>
<body>
<h1>FlakePilot</h1>
<p class="tagline">Reliably flag flaky tests: ingest your CI test reports,
classify failures, reproduce them deterministically, verify the fix &mdash; never
executing your repository's code inside the control plane.</p>
<p><a class="signin" href="/auth/github/login">Sign in with GitHub</a></p>
<p class="note">After signing in with a registered GitHub account you reach your
organization's runs directly; a stored run's classified view lives at
<code>/orgs/&lt;org&gt;/runs/&lt;run&gt;</code>. This page is static; the API's
health probes live under <code>/health/</code>.</p>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing() -> HTMLResponse:
    return HTMLResponse(_PAGE, status_code=200, headers=HTML_SECURITY_HEADERS)
