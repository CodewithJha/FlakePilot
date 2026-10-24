# Sentry observability boundary

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 7 (Sentry). Read-only boundary statement: what observability already
exists in the product, what would and would not ever leave the boundary if an
error-tracking service were adopted, and why no SDK is wired today. Nothing is
installed; no account is created; no telemetry leaves the machine.

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
the lockfile carry none, and no environment variable turns one on.

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

## Why nothing is wired today

- **An SDK is a third-party dependency and an egress path.** It would be the
  first component with standing to send process-adjacent data off the host,
  and green-lighting that requires the owner's go on vendor terms, the exact
  transmitted-field list, and the default-deny-egress review — all
  owner-gated actions this phase cannot take.
- **The need is already met locally.** `.env`-free JSON logs with request
  correlation give an operator everything aggregators index; adoption is
  additive later without touching any logging call site, because the SDK
  hook (a logging handler) sits exactly where `configure_logging` already
  installs the JSON handler.

Status: **boundary recorded, SDK deliberately absent**. Revisit only on
owner authorization of a specific provider with a written transmitted-field
review; the invariants above are non-negotiable in that review.
