# Browserbase browser evidence

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decisions 4
and 8 and sponsor goal 3 (Browserbase). This page says what is built, what is
not, and what the owner must supply before any live capture. Production wiring
is unchanged by this code: no composition root constructs a browser provider,
so the default product makes no browser call.

## What exists in code

| Piece | Location | Status |
|---|---|---|
| Port: `BrowserEvidenceProvider`, `BrowserCaptureRequest`, `BrowserEvidence`, `BrowserRefusal`, `BrowserCapture`, `BrowserAllowlist`, `target_host` | `integrations/application/browser.py` | Tested with mocks |
| Fixture: `RecordedBrowserEvidence` (canned pages, same allowlist and caps) | `integrations/infrastructure/browserbase.py` | Tested with mocks |
| Live adapter: `BrowserbaseEvidence` over the Browserbase Fetch API | `integrations/infrastructure/browserbase.py` | Tested with mocks; **live NOT VERIFIED** (no key) |
| Live check, skipped without a key | `tests/live/test_live_browser_speech.py` | Skipped (verifies nothing) |
| Investigation stage | none | Not wired (see "Not built") |

## The rules the port enforces

- **Allowlist before contact.** A target is captured only if it is `https`, on
  the default port, with no credentials in the URL, and its host is an exact
  member of the operator's `BrowserAllowlist`. IP literals, single-label names
  such as `localhost`, and private-use suffixes (`.local`, `.internal`,
  `.localhost`, `.arpa`, and others) cannot be allowlisted at all. A refused
  target produces a `BrowserRefusal("off_allowlist")` and no network call.
- **No redirects.** Browserbase is asked not to follow them
  (`allowRedirects: false`). A 3xx target status is refused
  (`redirect_refused`), never chased. The adapter's own call to Browserbase
  also refuses redirects.
- **Bounded session.** One `capture` call has at most 5 targets, a wall budget
  of 1–300 seconds (each transport timeout is the remaining budget), and a
  per-page byte cap of up to 5 MiB. Targets reached after the budget runs out
  are refused (`budget_exhausted`).
- **Provenance on every record.** Source URL, retrieval time (timezone-aware),
  session id, Browserbase's fetch id as `capture_id`, the target's HTTP status,
  content type, SHA-256 and size of the full retrieved body, and an excerpt
  capped at 8 KiB.
- **Content is untrusted data.** Excerpts are evidence text only. They grant
  nothing, carry no budget, and must enter any model prompt as framed data
  parts (the existing `AiDataPart` rule).
- **Failures are short codes.** A rejected key (`unauthorized`), rate limiting
  (`rate_limited`), an unreachable provider, a 5xx, or a malformed answer
  raises `BrowserEvidenceError` with no provider text. A non-2xx body is never
  read, and the API key appears only in the `X-BB-API-Key` header, never in
  errors (tested).

## Why Fetch rather than a browser session

Verified against vendor documentation on 2026-10-24:

- `POST https://api.browserbase.com/v1/fetch` with `X-BB-API-Key` retrieves one
  URL through Browserbase's infrastructure and returns `id`, `statusCode`,
  `headers`, `content`, `contentType`, and `encoding`. It does **not** execute
  JavaScript, has a 5 MB content limit and a 60-second target timeout (502/504
  on failure), and needs no session.
- A full browser session (`POST /v1/sessions`, then a CDP WebSocket at the
  session's `connectUrl`, then release) needs a WebSocket/CDP client. The
  repository has none, and ADR-017 decision 10 forbids adding one this phase.
  The Stagehand REST API can drive a session over plain HTTP, but reading page
  content goes through its LLM-backed `extract`, which adds a model-provider
  data flow. That is a separate consent decision and was not built.

Because Fetch holds no remote session, there is nothing to release. The
"release in `finally`" rule in the port binds future session-holding
providers. For Fetch, the session id is local (one per `capture` call), and
the only cleanup duty is to stop contacting the provider once a provider-level
error occurs (tested).

Request fields sent: `url`, `allowRedirects: false`, `allowInsecureSsl: false`,
`proxies: false`. `format` is omitted (raw), which is the cheapest Fetch tier;
markdown/JSON conversion is billed separately as "Fetch Extract".

## Not built (and why)

- **JavaScript-rendered capture and screenshots.** This needs a session plus a
  CDP client. A future dependency request would be either `websockets`
  (BSD-3-Clause, raw CDP) or `playwright` (Apache-2.0, `connect_over_cdp`).
  Either needs licence-gate review and an owner decision; neither is requested
  now.
- **An investigation `browse` stage.** Adding one changes the stage list and
  the provenance counts that the integration suite asserts
  (`tests/integration/test_investigation_postgres.py`). The intended shape is:
  run after `research`, capture only research-source URLs the allowlist
  permits, record `skipped/provider_absent` when unconfigured, add the
  evidence count to the provenance payload, and frame excerpts as `page` data
  parts within `max_evidence_parts`. This is left to an integrator who can
  update and run that suite.

## What the owner must supply

| Item | Where | Notes |
|---|---|---|
| Browserbase API key | Browserbase dashboard settings (<https://browserbase.com/settings>) | Fetch takes no project id; Browserbase documents keys as project-scoped |
| The host allowlist | Operator decision | Exact public hosts, e.g. `docs.python.org`, `github.com` |
| Data-flow consent | Owner | Target URLs (allowlisted public pages) go to Browserbase; no repository content is sent |
| Plan | Browserbase billing | Per the vendor plans page (2026-10-24): Free includes 1,000 Fetch calls/month with no overage; Developer ($20/mo) 1,000 then $1/1k; Startup ($99/mo) 10,000 then $0.50/1k; 5 req/s Fetch rate limit. Proxies cost more and are off |

Vendor sources: <https://docs.browserbase.com/platform/fetch/overview>,
<https://docs.browserbase.com/reference/api/fetch-a-page>,
<https://docs.browserbase.com/account/billing/plans>,
<https://docs.stagehand.dev/v3/api-reference/go/start-a-new-browser-session>.
