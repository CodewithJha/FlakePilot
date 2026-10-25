# Sentry observability boundary

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 7 (Sentry). Read-only boundary statement: what observability already
exists in the product, what would and would not ever leave the boundary if an
error-tracking service were adopted, and the opt-in reporter that now
implements it. No SDK is installed. Without
`FLAKEPILOT_SENTRY_DSN` no telemetry leaves the machine; the DSN is
deployment configuration held by the platform, never repository content.

## What the product already provides (Verified in source)

The observability primitive here is **structured JSON logs with request
correlation** — the substrate any error aggregator, Sentry included, would sit
on:

- **One JSON object per log line** (`JsonFormatter`,
  `interface/composition_root/__init__.py`): fixed fields `timestamp`,
  `level`, `logger`, `message`, `request_id`; optional `client_request_id`
  (a validated inbound `X-Request-ID`, logged on the request line only) and
  `exception` (a server-side traceback, never returned to a client).
- **Request IDs are always ours** (`RequestContextMiddleware`,
  `interface/http/request_context.py`): uuid4, returned in `X-Request-ID`. A
  client-supplied ID is never adopted as ours — only logged as
  `client_request_id` after passing a strict allow-list pattern.
- **The request line leaks no path values.** It logs the matched route
  template (no IDs, no tokens, no query string), truncated to a bounded
  length when no template exists.
- **Failures are logged as classes, not payloads.** Startup failures and
  worker stoppages log `type(exc).__name__` — e.g. "worker stopped:
  OperationalError" — so the log carries no credential, URL, or exception
  text that might embed one. Unhandled HTTP exceptions log the traceback
  server-side; the client body stays generic.
- The worker, relay, and HTTP layers all log through the same root JSON
  handler, so a single log stream covers both deployed processes.

No Sentry SDK (or any telemetry SDK) is a dependency; `pyproject.toml` and
the lockfile carry none. Since the sponsor phase, one opt-in sink exists —
see "The reporter that exists" below.

## The boundary an error tracker could never cross

If, and only if, an owner later authorizes an error-tracking dependency
(Decision 10's vendor gate: terms, licence purity, transmitted fields,
data-flow review), these rules are the contract it signs up to — they are the
log discipline above, applied to export:

1. **Identifiers only.** Org and repository IDs, request IDs, event names,
   and counts. The same payload contract the event envelope enforces
   (ADR-012): IDs and enum-shaped values, never free text authored outside
   the product.
2. **No exception strings from the worker path.** Worker failures export the
   exception class only — matching what the product itself logs.
3. **No untrusted content.** Repository names, branch names, artifact keys,
   evidence parts, model/provider text, request/response bodies: these are
   untrusted data under [EXECUTION-HOST](EXECUTION-HOST.md)'s rule and the
   ADR-017 data-flow posture ("external content is data, never a resource").
   They are exactly the strings an aggregator's "breadcrumbs" would happily
   capture by default, and exactly what this boundary refuses.
4. **No secrets, ever.** `Secret` values are redacted from `repr`, `str`, and
   logs by construction, so there is no code path that could hand one to a
   telemetry sink (the value exists only at the point of `reveal()` use).
5. **The request-ID spine is the correlation key.** Any exported event
   correlates by `request_id`/`client_request_id`; nothing else is needed to
   join an error report back to the local JSON log line, which retains full
   server-side detail.

## The reporter that exists (opt-in, standard library)

`interface/composition_root/error_reporting.py` installs one logging handler
next to the JSON handler, in the API process only (`create_app`), **only when
`FLAKEPILOT_SENTRY_DSN` is set**; unset, no handler exists and nothing leaves
the process. The worker gets no provider setting at all, this one included:
its errors stay in its JSON log. The HTTP side is
`integrations/infrastructure/sentry.py` (outbound HTTP stays in integration
adapters, ADR-003): it posts Sentry envelopes
(`POST https://<host>/api/<project>/envelope/`, `X-Sentry-Auth` with the DSN's
public key) over the standard library — TLS verified, redirects refused,
5-second timeout, bounded queue, events dropped rather than retried.

It implements the boundary above by construction: events are **built** from
an allow-list, not scrubbed from a full capture. The complete transmitted-field
list is:

| Field | Content |
|---|---|
| `event_id`, `timestamp`, `platform`, `level`, `release` | generated; `level` is `error`/`fatal` (ERROR and CRITICAL records only) |
| `logger` | logger name |
| `logentry.message` | the log call's **template** (`"worker stopped: %s"`), never its arguments; replaced by `[redacted]` if it looks like it carries a credential |
| `exception.values[0]` | class name and module; frames as file *name*, function, line, in-app flag. Never the exception text, source lines, absolute paths, or local variables |
| `tags` | `request_id` (ours, uuid4) and, when a log call passes them as `extra=`, `route` (matched template only), `org_id`, `repo_id`, `investigation_id`, `stage` — each pattern-checked; anything credential-shaped is dropped |

Never collected: requests, headers, cookies, query strings, bodies,
breadcrumbs, users, IP addresses, host names, environment, argv, installed
modules, client-supplied request IDs. Tests capture the outbound HTTP request
at the transport and assert that a seeded API key, session cookie, bearer
token, query secret and body secret, raised in an exception, sent on a
request, passed as log arguments and as `extra=`, never appear anywhere in it.

One consequence an operator should know: a 500 inside a request produces
exactly one event, tagged with the same `request_id` the client saw in
`X-Request-ID`.

## Why the Sentry SDK is not used

`sentry-sdk` (MIT) hard-depends on `certifi`, which is **MPL-2.0**. The
runtime licence gate (`pip-licenses --python .venv-runtime/bin/python`)
allows no exceptions, and the repository already avoids `httpx` for the same
reason. Adding the SDK would either fail that gate or require an owner-approved
runtime exception. The SDK would also capture request data, breadcrumbs, argv
and modules by default — every one of which this boundary forbids — so the
standard-library reporter is both the licence-clean and the narrower path.
Switching to the SDK later is an owner decision: approve a runtime `certifi`
exception, then keep the allow-list rules above as its `before_send`.

## What remains owner-gated

- ~~Creating the Sentry account/project and its DSN~~ **Done by the owner
  (2026-10-25)**: a DSN is configured on the deployed API (its startup log
  shows `error_reporting=sentry`). The vendor terms, residency and retention
  choices on that project are the owner's, made outside this repository.
- The egress review: on a default-deny-egress host, the Sentry ingest host
  must be explicitly allowed.

## Verified where, and not verified where

- **Live-verified locally (2026-10-24):** one synthetic allow-listed event
  posted by this exact reporter was accepted by the real ingest endpoint
  (HTTP 200, same event id echoed) — plumbing and field shape only.
- **Deployed (2026-10-25):** the reporter is configured on
  `app.flakepilot.tech`'s API process. **No error event from the deployment
  has been observed** — expected while no ERROR/CRITICAL has occurred there,
  not evidence either way about delivery from Render's egress. First
  real event, or the smoke-test check #12 (a known 500 in staging only),
  is what would verify the deployment path.

Status: **opt-in reporter implemented and tested with a capturing transport
(Tested with mocks); envelope transport live-verified locally 2026-10-24;
configured on the deployment, no event from it observed yet; SDK not adopted
(licence gate)**. The invariants above remain non-negotiable for any future
change.
