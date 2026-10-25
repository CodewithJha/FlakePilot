# FlakePilot Mastra sidecar

A small [Mastra](https://mastra.ai/) workflow that asks FlakePilot to investigate
one stored run, as an Auth0 machine-to-machine client. FlakePilot stays the
only authority: this process is a client of FlakePilot's agent route, not a
second workflow engine. It has no database access and no FlakePilot
credential. It holds one short-lived Auth0 token for its own client, and
FlakePilot checks that token against an admin-made binding on every call.

```
obtain-token ──▶ investigate ──▶ validate ──▶ summarize
(Auth0 client     (POST /agent/orgs/{org}/runs/{run}/investigation,
 credentials)      one call, Bearer token)       (zod)     (report object)
```

- **Budgets.** At most one investigation per invocation: each run builds a
  fresh workflow, and that workflow refuses a second call. Each outbound call
  has a timeout. Nothing is retried, so a 4xx (or any other failure) is final.
- **No secrets in output.** The token never enters a step's input or output.
  It sits in a per-invocation holder and is dropped after its single use.
  Errors are short codes such as `token_http_401`, `flakepilot_http_404`,
  `flakepilot_timeout`, or `schema_invalid`, never response text.
  Mastra's step-error log lines go to stderr and carry only those codes.
- **No telemetry.** `src/main.ts` sets `MASTRA_TELEMETRY_DISABLED=1` before
  Mastra loads, and the workflow never constructs a `Mastra` instance.
- **Advisory only.** The report summarizes what FlakePilot already returned.
  FlakePilot's verdicts come from its deterministic rule set, and model output
  arrives as counts. The report says `"authority": "flakepilot"`.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `FLAKEPILOT_BASE_URL` | FlakePilot origin. https, or plain http only to a loopback host |
| `FLAKEPILOT_AUTH0_DOMAIN` | Auth0 tenant host, e.g. `tenant.us.auth0.com` |
| `FLAKEPILOT_AUTH0_AUDIENCE` | The FlakePilot API identifier in Auth0 |
| `FLAKEPILOT_AGENT_AUTH0_CLIENT_ID` / `_SECRET` | This agent's M2M application, authorized for `repo.investigate` (and optionally `repo.view`) |
| `FLAKEPILOT_AGENT_TOKEN_TIMEOUT_MS` | Token call timeout, default 10000 |
| `FLAKEPILOT_AGENT_INVESTIGATION_TIMEOUT_MS` | Investigation call timeout, default 180000 |

FlakePilot must have `FLAKEPILOT_AUTH0_DOMAIN`/`_AUDIENCE` set, which mounts
the agent route. An org admin must also bind this client's subject
(`<client_id>@clients`) with the `bind-agent` operator command (see the
repository README).

## Use

```sh
npm ci
npm test                      # tsc --noEmit, then node:test against a mocked FlakePilot and token endpoint
node src/main.ts --org <org-uuid> --run <run-uuid>
```

The command prints one JSON line: `{"ok":true,"report":{...}}` (exit 0) or
`{"ok":false,"error":"<code>"}` (exit 1). Node ≥ 22.18 runs the TypeScript
directly (type stripping), so there is no build step.

## Licences

Only permissively licensed code is used, and only from the
`@mastra/core/workflows` entry point. `@mastra/core` is Apache-2.0 except
content under any `ee/` directory (Mastra Enterprise License). The package
ships `dist/auth/ee` and `dist/agent-builder/ee`. Nothing here imports them,
and a test fails if a source file imports any `@mastra/*` path other than
`@mastra/core/workflows`.

The installed tree (`package-lock.json`, 149 packages) contains MIT,
Apache-2.0, ISC, BSD-2-Clause, BSD-3-Clause, and BlueOak-1.0.0 (`lru-cache`,
`@isaacs/ttlcache`). It also contains `json-schema@0.4.0` under
`AFL-2.1 OR BSD-3-Clause`; BSD-3-Clause is elected. Direct dependencies:
`@mastra/core` 1.79.0 (Apache-2.0, `ee/` excluded), `zod` 4.6.7 (MIT). Dev
dependencies: `typescript` 5.9.3 (Apache-2.0) and `@types/node` 26.6.5 (MIT).
