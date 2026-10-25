# Auth0 agent route and bindings (goal 9)

How an AI agent with an Auth0 machine-to-machine token can ask FlakePilot for
one bounded investigation, and how an org admin controls that. Background:
[ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 6,
`identity/application/agents.py`, `agent_tokens.py`, and `agent_bindings.py`.

## Trust model in one paragraph

An Auth0 token is evidence about the agent software, never authority. The
only authority is a **binding** that an org admin made: one Auth0 subject (the
token `sub`, e.g. `<client_id>@clients`), one repository, scopes within
`{repo.view, repo.investigate}`, and a TTL of at most 8 hours. On every
request FlakePilot recomputes the agent's grants from the delegating admin's
*current* membership and grants, under the admin's *verified* code-host
standing. It intersects them with the binding, narrows them by the token's
scopes and expiry, and only then authorizes. Removing or demoting the admin,
a lost code-host standing, unbinding, or expiry each end the agent's reach.
Agents can never approve, configure, or grant: the scope cap makes those
grants unreachable, and the agent surface is one investigation route.

## Enable it

FlakePilot (API process only; the worker builds no providers):

```sh
export FLAKEPILOT_AUTH0_DOMAIN=tenant.us.auth0.com
export FLAKEPILOT_AUTH0_AUDIENCE=https://api.flakepilot.example
```

With both unset the route is not mounted: the path answers exactly like an
unknown route (404), and `provider-status` reports `agent_token_issuer=none`.
With both set it reports `agent_token_issuer=auth0`.

## Bind and unbind (operator channel, on behalf of an org admin)

```sh
python -m flakepilot.interface.composition_root bind-agent \
  --org-id <org-uuid> --admin-github-id <numeric-id> --repo-id <repo-uuid> \
  --auth0-subject '<client_id>@clients' --agent-name mastra_triage \
  --scope repo.investigate --scope repo.view --ttl-minutes 60
# bound <binding-uuid> expires_at=<timestamp>

python -m flakepilot.interface.composition_root unbind-agent \
  --org-id <org-uuid> --admin-github-id <numeric-id> --auth0-subject '<client_id>@clients'
# revoked   (a repeat prints not_bound)
```

- Only an org admin is accepted, and the repository must be registered in the
  org. Like `set-ai-policy`, the operator acts for that admin, and the audit
  event's actor is the admin.
- At most one unrevoked binding per (org, subject). Binding again needs an
  unbind first, even after the binding expired.
- Stored in `iam.agent_bindings` (migration `iam_0003`). Only the subject's
  SHA-256 is stored. Rows cascade away with the admin's membership. Database
  checks repeat the scope cap and the 8-hour TTL.
- Audited as `iam.agent.bound` and `iam.agent.unbound` in `iam.outbox`. The
  events carry IDs, the agent code, grant count, TTL, and expiry, never the
  subject.
- The operator channel has no per-user code-host evidence, so the binding
  rules are checked under the deployment's read standing. Each request
  re-checks them under the admin's verified standing.

## Call it

```
POST /agent/orgs/{org_id}/runs/{run_id}/investigation
Authorization: Bearer <Auth0 access token for FLAKEPILOT_AUTH0_AUDIENCE>
```

Bearer only: cookies are never read, so there is no ambient credential and no
CSRF surface. The 200 body is the session route's investigation JSON, with
`Cache-Control: no-store`. The agent surface has no audio route.

| Situation | Answer |
|---|---|
| Missing, malformed, wrong-audience, expired, tampered, or otherwise refused token | 401 `unauthenticated`, identical bodies, no reason |
| No active binding for the token subject in this org; revoked or expired binding; admin removed or demoted; run unknown or in another repository; delegator standing unverified | 404 `not_found`, identical to an unknown route |
| Visible run, but the token or binding lacks `repo.investigate` | 403 `forbidden` |
| Auth0 not configured | 404 (route not mounted) |

Provenance: a completed investigation writes one `diag.investigation.recorded`
event. Its actor is the delegating admin, and the payload carries
`via_agent=<agent code>`. The workflow's own gates still apply unchanged:
`repo.investigate`, the org AI policy, and stage budgets. Refusals before the
workflow (401, 404, the 403 from narrowing) write no provenance, the same as
the session route's pre-workflow refusals.

## Evidence (2026-10-25)

- **VERIFIED:** unit tests (`tests/unit/identity/application/test_agent_bindings.py`,
  `tests/unit/interface/test_agent_investigations_http.py`) and real-PostgreSQL
  integration tests (`tests/integration/test_agent_route_postgres.py`). They
  cover binding storage, the audit events, revocation, the schema caps, the
  CLI, every refusal status, non-disclosure parity, provenance attribution,
  and lifespan wiring.
- **VERIFIED, local:** `tests/live/test_live_auth0_route.py`. One real
  client-credentials token from the development tenant went through the route
  in-process against a throwaway database: 200 `complete` with attributed
  provenance, a tampered copy answered 401, and the same token answered 404
  after unbind.
- **Stand-in, not evidence:** in those tests the delegator's code-host
  standing comes from a `verified_read` stand-in. The GitHub App verifier on
  this path is **NOT VERIFIED**.
- Nothing is deployed.
