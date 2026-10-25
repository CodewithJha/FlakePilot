# Deployment runbook — Render (sponsor goals 10, 7, 9, 16)

Companion to [RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)
(the shape and its reasoning) and [ADR-017](../adr/ADR-017-sponsor-capability-phase.md).
This page is the operator's step list: what to create, what to set, how to
check it, how to undo it, and the record of the one deployment that exists
([Deployed state](#deployed-state-2026-10-25)). Render behavior this page does
not list as observed there is vendor documentation, not tested here.

Execution stays closed on every service this runbook creates: the image has no
container runtime, the worker runs `ClosedSandbox` / `UnavailableIsolation` /
`control_plane_reaper()` / `RefusedHostHandoff`, and no environment variable in
this page changes that. A Render service is not an execution host.

## What the repository provides

| File | What it is | Status |
|---|---|---|
| `Dockerfile` | One image for API and worker: digest-pinned `python:3.12-slim` and `uv` 0.11.23, `uv sync --frozen --no-dev --no-editable`, non-root user `flakepilot` (uid 10001), binds `0.0.0.0:10000`, no secrets, no repository code beyond `src/` | **VERIFIED** — built by Render on every deploy since 2026-10-25 (never built locally: no Docker daemon) |
| `.dockerignore` | Allow-list: `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, `src/` only | Present |
| `render.yaml` | Blueprint: web service `flakepilot-api`, worker `flakepilot-worker`, Postgres 16 `flakepilot-db` (private network only), all in `singapore` | Matches the deployed services; the resources were created through the Render API, so **Blueprint sync itself is NOT VERIFIED** |

## Costs (vendor list prices; deployed at these plans, no invoice checked)

| Resource | Plan | List price |
|---|---|---|
| `flakepilot-api` | `0.5c-512mb` (paid: disks and pre-deploy commands are paid-only) | USD 7/month |
| `flakepilot-worker` | `0.5c-512mb` | USD 7/month |
| `flakepilot-db` | `0.1c-256mb`, 1 GB storage included | USD 6/month |
| Artifact disk | 1 GB | USD 0.25/month |
| Hobby workspace | 2 custom domains, 5 GB bandwidth included | USD 0 |
| **Total** | | **≈ USD 20.25/month** + bandwidth beyond 5 GB |

## Owner decisions

None can be made by an agent. All four were made for the 2026-10-25 deployment.

1. **D-HOST-1/2/5: Render hosting and the ≈ USD 20/month spend** — approved.
2. **D-HOST-3: region** — `singapore` for all three resources; region is
   immutable after creation.
3. **Domain**: `app.flakepilot.tech`, see
   [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md).
4. **GitHub App** with callbacks for that origin (sign-in and code-host
   verification); without it sign-in answers 503 and session views answer 404 —
   both fail closed, so a first deploy without it is safe.

## Deploy steps

1. Render Dashboard → **New → Blueprint** → connect `CodewithJha/FlakePilot`,
   branch `main`. Render reads `render.yaml`.
2. Render prompts for every `sync: false` variable. **Leave all of them empty
   for the first deploy** (empty means unset; every such feature stays off).
   Note: Render prompts for these only at Blueprint creation; later additions are
   set by hand on each service's Environment page.
3. Apply. Render creates the database, builds the image, runs the API's
   pre-deploy command `env -u FLAKEPILOT_ARTIFACT_DIR alembic upgrade head` on
   separate compute, then starts
   both services. On the very first deploy the worker may start before the
   migration finishes; it then exits on the missing tables and Render restarts
   it (no work exists yet, nothing is lost).
4. Wait for the API deploy to go live. Render's health check is
   `GET /health/live`; the port only opens after the API's bounded
   database `SELECT 1` succeeds at startup, so "live" already implies the
   database answered once.
5. Run the smoke tests below. Only then add secrets (step 6).
6. Add the configuration that turns features on, one group at a time, each
   followed by its smoke test: public origin + GitHub App (sign-in), Sentry DSN
   (error reporting), Auth0 domain + audience (parsed only, see the table).
   Each environment change redeploys the service.
7. Custom domain: API service → Settings → Custom Domains → add the host, then
   create the DNS record from [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md);
   Render issues and renews the TLS certificate. Then set
   `FLAKEPILOT_PUBLIC_ORIGIN=https://<host>` and register
   `https://<host>/auth/github/callback` on the GitHub App.

### Schema changes after the first deploy

Both services auto-deploy on a commit whose CI checks pass
(`autoDeployTrigger: checksPass`). Only the API runs migrations — two concurrent
`alembic upgrade` runs could race on `alembic_version`. The release discipline
from [RELEASE-CANDIDATE](RELEASE-CANDIDATE.md) applies: a migration must accept
the old and the new code. If a new worker build starts before the API's
pre-deploy migration completes, it fails on startup and Render restarts it;
claimed jobs are leased, so the effect is delayed work, never lost work.

## The artifact disk, honestly

- Render disks attach to **one** service instance, are reachable **only at
  runtime** (not in build, pre-deploy, or one-off jobs), prevent scaling that
  service beyond one instance, disable zero-downtime deploys (a few seconds of
  downtime per deploy) and fix the shutdown delay.
- The disk is on the API because only the API writes artifacts today: JUnit
  ingestion (`POST /ingest/junit`). In the closed execution posture the worker
  stores no evidence (`ClosedSandbox` produces none; with no artifact store the
  handler records nothing), so the worker has no `FLAKEPILOT_ARTIFACT_DIR`.
- If execution ever opens, the worker would need to write evidence the API
  serves — a Render disk cannot be shared, so that change needs an object store
  or the dedicated execution host's own storage (an owner/ADR decision, not a
  config edit).
- Artifacts on the disk are plaintext files to the application (Render
  encrypts disks at rest at the platform layer; that is not the product's
  encryption at rest), never cleaned up, and snapshotted daily by Render for 7
  days. Retention remains a human decision outside this repository.
- **VERIFIED (2026-10-25):** a fresh Render disk mount is writable by the
  non-root user: the API start command created `/var/data/artifacts` with
  mode 700 and two JUnit uploads were stored there. If a future mount is not
  writable, `mkdir` fails and the API does not start; the fallback is to
  remove `FLAKEPILOT_ARTIFACT_DIR` from the API (and the `mkdir` from
  `dockerCommand`), which leaves ingestion answering 503
  `ingestion_not_configured`.
- The disk is not mounted during the pre-deploy command, so migrations run
  with `FLAKEPILOT_ARTIFACT_DIR` unset (`env -u`); otherwise settings
  validation would refuse the missing artifact root.

## Environment variables

"Parsed" means `Settings.from_environ` validates it (a bad value is a startup
refusal naming the variable, never its value). "Wired opt-in" means the API's
composition root builds the adapter only when the variable is set; unset means
no adapter and no contact. Every provider variable belongs to the API service
only. After setting any, check the startup log line `hosted providers
configured (not checked at runtime): …` or run `provider-status` in the shell.

| Variable | Service | Secret | Source / value | Status |
|---|---|---|---|---|
| `FLAKEPILOT_DATABASE_URL` | API, worker | yes | `fromDatabase` connection string (private network) | Parsed, wired |
| `FLAKEPILOT_HOST` / `FLAKEPILOT_PORT` | both | no | `0.0.0.0` / `10000`, set in the image | Parsed, wired |
| `FLAKEPILOT_LOG_LEVEL` | both | no | `INFO` | Parsed, wired |
| `FLAKEPILOT_EXECUTION_MODE` | both | no | `closed` (also the default) | Parsed; worker runs closed only |
| `FLAKEPILOT_ARTIFACT_DIR` | API | no | `/var/data/artifacts` on the disk | Parsed, wired |
| `FLAKEPILOT_PUBLIC_ORIGIN` | API | no | `https://app.flakepilot.tech` (in `render.yaml`), exactly as browsers send `Origin` | Parsed, wired; set on the deployment |
| `FLAKEPILOT_GITHUB_CLIENT_ID` / `_CLIENT_SECRET` | API | secret: yes | GitHub App | Parsed, wired (sign-in); set; live sign-in **VERIFIED** 2026-10-25 |
| `FLAKEPILOT_GITHUB_APP_ID` / `_APP_PRIVATE_KEY` | API | key: yes | GitHub App | Parsed, wired (code-host verification); set; the session-view verifier **VERIFIED** live 2026-10-25 (run view loaded) |
| `FLAKEPILOT_SENTRY_DSN` | API | yes | Sentry project DSN (`https://<key>@<host>/<project>`) | Parsed, wired (opt-in reporter); set; no event from the deployment observed |
| `FLAKEPILOT_AUTH0_DOMAIN` / `_AUDIENCE` | API | no | Auth0 tenant host / API identifier | Parsed, wired: mounts the agent route; set (startup log `agent_token_issuer=auth0`); the route is **NOT VERIFIED** on the deployment |
| `FLAKEPILOT_AI_ENDPOINT` / `_AI_MODEL` / `_AI_API_KEY` | API | key: yes | operator model endpoint | Parsed, wired (`LocalEndpointAssist`); not set |
| `FLAKEPILOT_GEMINI_API_KEY` / `_GEMINI_MODEL` | API | key: yes | Google AI Studio key + `gemma-4-31b-it` or `gemma-4-26b-a4b-it`; not with `FLAKEPILOT_AI_ENDPOINT` | Parsed, wired opt-in (hypothesize stage); set; **VERIFIED** on the deployment 2026-10-25 (hypothesize `produced`) |
| `FLAKEPILOT_SERPAPI_API_KEY` | API | yes | SerpApi | Parsed, wired opt-in (research stage); set; **VERIFIED** on the deployment 2026-10-25 |
| `FLAKEPILOT_BROWSERBASE_API_KEY` / `FLAKEPILOT_BROWSER_ALLOWLIST` | API | key: yes | Browserbase key + comma-separated public host names | Parsed, wired opt-in (browse stage); not set; live **NOT VERIFIED** |
| `FLAKEPILOT_ELEVENLABS_API_KEY` / `_VOICE_ID` (`_MODEL`) | API | key: yes | ElevenLabs | Parsed, wired opt-in (spoken brief route); set; **VERIFIED** on the deployment 2026-10-25 |
| `FLAKEPILOT_TABPFN_API_KEY` | API (operator shell) | yes | Prior Labs | Parsed, used only by `evaluate-prioritization --with-tabpfn`; not set on the deployment |
| `FLAKEPILOT_ATLAS_URI` / `_DATABASE` | API (operator shell) | yes | MongoDB Atlas, together | Parsed, read only by `export-reports-atlas` (write-only copy; never read back). Declared in `render.yaml` 2026-10-25 so the command can run from the service's Render Shell against the deployed PostgreSQL; not set on the deployment, so Atlas is never contacted |

Never set `FLAKEPILOT_ALLOW_INSECURE_COOKIES=true` on Render (TLS is served;
`__Host-` + `Secure` stay on). Never give the worker GitHub, session, provider or
Auth0 secrets; its only credential is the database URL.

## Smoke tests

`$API` is `https://<service>.onrender.com` (or the custom domain). Commands
marked *shell* run in the API service's Render Shell (the image puts the venv on
`PATH`; the working directory holds `pyproject.toml`).

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Liveness | `curl -si $API/health/live` | `200 {"status":"ok"}`, an `X-Request-ID` header |
| 2 | Readiness (database) | `curl -si $API/health/ready` | `200 {"status":"ready"}` |
| 3 | Migration state | *shell* `alembic current` then `alembic check` | current = head; check reports no differences |
| 4 | Sign-in off (before GitHub config) | `curl -si $API/auth/github/login` | `503`, code `auth_not_configured` |
| 5 | Sign-in on (after) | browser → `$API/auth/github/login` | round-trip to GitHub and back; cookie has `__Host-` prefix and `Secure` |
| 6 | Bootstrap + token | *shell* `python -m flakepilot.interface.composition_root bootstrap-org --name SmokeOrg --admin-github-id <id> --admin-login <login>`, then `issue-ci-upload-token --org-id $ORG_ID --repo-id $REPO_ID` | org ID printed; token printed once |
| 7 | Ingest | from a checkout: README "MVP demo journey" step 5 `curl` with `localhost:8000` replaced by `$API` | `201` with upload ID, run ID, counts; a file appears under `/var/data/artifacts` |
| 8 | Run discovery | *shell* `python -m flakepilot.interface.composition_root list-runs --org-id $ORG_ID --repo-id $REPO_ID --format json` | the uploaded run, newest first |
| 9 | Report | *shell* `python -m flakepilot.interface.composition_root ci-run-report --org-id $ORG_ID --repo-id $REPO_ID --run-id $RUN_ID --format json` | verdicts with evidence; revision `unchecked` |
| 10 | Worker drain | Dashboard → worker → Restart | log line sequence ends cleanly; a claimed job completes after lease expiry if killed |
| 11 | Execution posture | worker logs at start | `runtime reaper: runtime_reaper_unavailable`; no container, no repository code |
| 12 | Error reporting (after DSN) | trigger a known 500 in staging only (the worker has no reporter) | one Sentry event: exception class, frames, `request_id`/`route` tags; no message arguments, headers, cookies, query or body |

Delete the smoke org's data only through the product's own paths; never delete
audit or outbox rows.

## Rollback

- **Code:** Dashboard → service → Events → previous deploy → **Rollback**
  (Hobby keeps the last 5 builds). Roll back the worker and API to the same
  commit. Rolling back code never rolls back the schema; the
  migrate-first rule is what makes the previous build compatible.
- **Schema:** there is no automated downgrade step in this runbook. A bad
  migration is fixed forward, or restored with Render Postgres point-in-time
  recovery (3-day window on Hobby) into a **new** database, then repointing
  `FLAKEPILOT_DATABASE_URL` — an owner decision, since it discards writes.
- **Artifact disk:** Render restores whole-disk daily snapshots (7 days);
  everything after the snapshot is lost.
- **Feature flags:** clearing a `sync: false` variable turns its feature off
  (empty means unset) and redeploys the service.
- **Full stop:** suspend both services (billing stops for compute; database
  and disk persist and still bill).

## Deployed state (2026-10-25)

One Render workspace, region `singapore`, resources created through the Render
API to match `render.yaml` (IDs and connection strings are not recorded here).
The custom domain `app.flakepilot.tech` is verified and sign-in starts at
`https://app.flakepilot.tech/auth/github/login`.

| Resource | Plan | Notes |
|---|---|---|
| `flakepilot-db` | PostgreSQL 16, `0.1c-256mb`, 1 GB | `ipAllowList: []` (no public connections); opened to one `/32` only for operator commands, then closed and re-checked |
| `flakepilot-api` | `0.5c-512mb`, 1 GB disk at `/var/data` | health check `/health/live`, pre-deploy migrations, `autoDeployTrigger: checksPass`; holds the GitHub App, Gemini, SerpApi, ElevenLabs, Sentry and Auth0 variables |
| `flakepilot-worker` | `0.5c-512mb` | only `FLAKEPILOT_DATABASE_URL`, `FLAKEPILOT_LOG_LEVEL`, `FLAKEPILOT_EXECUTION_MODE=closed`; no provider, GitHub or session secret |

Estimated cost ≈ USD 20.25/month (7 + 7 + 6 + 0.25 at list prices; no invoice
checked). Execution stays closed: the worker logs
`runtime reaper: runtime_reaper_unavailable`, and no repository code runs.

**Two `render.yaml` defects found by the first deploy, both fixed:**

1. `dockerCommand: sh -c '…'` — Render passes the single quotes through, so
   `sh` looked for a program literally named `mkdir -p -m 700 … && exec …`
   and the API exited 127 (`not found`). Fixed by Render's documented
   unquoted form `/bin/sh -c mkdir -p -m 700 /var/data/artifacts && exec python -m flakepilot.interface.composition_root`.
2. `preDeployCommand: alembic upgrade head` — the migration environment loads
   the full settings, which validate `FLAKEPILOT_ARTIFACT_DIR`, and the disk
   is not mounted during pre-deploy. Found in the source and avoided before it
   failed (so the failure itself is **NOT VERIFIED**); fixed with
   `env -u FLAKEPILOT_ARTIFACT_DIR alembic upgrade head`, which has completed
   on every deploy since.

**Smoke results (VERIFIED, 2026-10-25):**

- Health: `/health/live` 200 `{"status":"ok"}` with `X-Request-ID`;
  `/health/ready` 200. Migrations at head, `alembic check` clean.
- Before GitHub configuration, every session route answered 503
  `auth_not_configured`.
- Ingestion over HTTPS with an operator-issued token: two uploads 201, an
  exact replay 200 `duplicate: true`, the same key with other content 409
  `idempotency_conflict`; missing and bad tokens 401. `list-runs` and
  `ci-run-report` (JSON and HTML) rendered the stored runs.
- Worker: no restarts; published every outbox row within about 2 s
  (8 published, 0 dead, 8 audit events at the time).
- After GitHub configuration: the owner signed in with GitHub on
  `app.flakepilot.tech` and the run view loaded (the session-view code-host
  verifier's first live check). `POST /orgs/{org}/runs/{run}/investigation`
  returned 200 `complete` (SerpApi research produced 5 sources, browse
  `provider_absent`, hypothesize `policy_disabled`), and
  `POST …/investigation/brief` returned 200 `audio/mpeg`.
- Gemma on the deployment: after `set-ai-policy --level evidence_only` (run
  as an operator command against the deployed database, owner-approved data
  flow), one investigation from the signed-in browser returned 200
  `complete`: collect, research (5 sources), hypothesize and prioritize
  `produced`, browse skipped `provider_absent`; assist `produced` with 2
  evidence-linked claims, 0 unverified, 0 contradicted (about 111 s).

**Deployed revision `ef96883` (2026-10-25, later the same day) — cost
controls and backlog visibility.** CI SUCCESS (run 40185346401); Render
auto-deployed it (`autoDeployTrigger: checksPass`); pre-deploy migrations
completed; the deployment answered `/health/live` 200, `/health/ready` 200,
and the new classification route 401 without a session (fail closed, same as
every session route; the ingest route stayed 401 without a token). The new
`backlog-status` command runs only where `FLAKEPILOT_DATABASE_URL` exists
(operator shell or Render shell); the API and worker behavior changes are
limited to the two cost controls, both on by default (120 uploads/hour per
token, 500 AI requests/day per org) and both overridable by environment
variable without a deploy. Owner actions are unchanged.

## What this runbook does not claim

- `docker build` was never run locally; the image is verified only by Render's
  builds.
- No isolation, encryption-at-rest, or production-readiness claim: Render
  hosting changes none of the evidence levels in
  [PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md).
