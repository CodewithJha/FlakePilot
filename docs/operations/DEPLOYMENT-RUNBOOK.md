# Deployment runbook — Render (sponsor goals 10, 7, 9, 16)

Companion to [RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)
(the shape and its reasoning) and [ADR-017](../adr/ADR-017-sponsor-capability-phase.md).
This page is the operator's step list: what to create, what to set, how to
check it, how to undo it. **Nothing here has been run.** No account, service,
database, domain or credential exists; every step is an owner action and every
Render-specific behavior is vendor documentation, not tested here.

Execution stays closed on every service this runbook creates: the image has no
container runtime, the worker runs `ClosedSandbox` / `UnavailableIsolation` /
`control_plane_reaper()` / `RefusedHostHandoff`, and no environment variable in
this page changes that. A Render service is not an execution host.

## What the repository provides

| File | What it is | Status |
|---|---|---|
| `Dockerfile` | One image for API and worker: digest-pinned `python:3.12-slim` and `uv` 0.11.23, `uv sync --frozen --no-dev --no-editable`, non-root user `flakepilot` (uid 10001), binds `0.0.0.0:10000`, no secrets, no repository code beyond `src/` | **NOT VERIFIED** — no local Docker daemon was running, so `docker build` was not executed. The wheel build (`uv build --wheel`) was verified to ship every migration directory and `script.py.mako` |
| `.dockerignore` | Allow-list: `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, `src/` only | Present |
| `render.yaml` | Blueprint: web service `flakepilot-api`, worker `flakepilot-worker`, Postgres 16 `flakepilot-db` (private network only) | Parses as YAML; **not validated by Render** (no account) |

## Costs (vendor list prices, 2026-10-24, NOT VERIFIED by purchase)

| Resource | Plan | List price |
|---|---|---|
| `flakepilot-api` | `0.5c-512mb` (paid: disks and pre-deploy commands are paid-only) | USD 7/month |
| `flakepilot-worker` | `0.5c-512mb` | USD 7/month |
| `flakepilot-db` | `0.1c-256mb`, 1 GB storage included | USD 6/month |
| Artifact disk | 1 GB | USD 0.25/month |
| Hobby workspace | 2 custom domains, 5 GB bandwidth included | USD 0 |
| **Total** | | **≈ USD 20.25/month** + bandwidth beyond 5 GB |

## Owner decisions that must exist first

These are the gates; none can be made by an agent.

1. **D-HOST-1/2/5: approve Render hosting and the ≈ USD 20/month spend.**
2. **D-HOST-3: pick the region.** `render.yaml` says `oregon` for all three
   resources; region is immutable after creation — edit before the first sync.
3. **Domain** (optional for a first smoke, required for sign-in on a real
   name): see [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md).
4. **GitHub App** registration for that origin (sign-in and code-host
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
   pre-deploy command `alembic upgrade head` on separate compute, then starts
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
- **NOT VERIFIED:** the ownership of a fresh Render disk mount. The API start
  command creates `/var/data/artifacts` with mode 700 as the non-root user;
  if the mount root is not writable by uid 10001, `mkdir` fails and the API
  does not start. Fallback: remove `FLAKEPILOT_ARTIFACT_DIR` from the API (and
  the `mkdir` from `dockerCommand`) — the API then starts with ingestion
  answering 503 `ingestion_not_configured` — and raise it with Render support.

## Environment variables

"Parsed" means `Settings.from_environ` validates it (a bad value is a startup
refusal naming the variable, never its value). "Wired opt-in" means the API's
composition root builds the adapter only when the variable is set; unset means
no adapter and no contact. Every provider variable belongs to the API service
only. After setting any, check the startup log line `hosted providers
configured (none verified live): …` or run `provider-status` in the shell.

| Variable | Service | Secret | Source / value | Status |
|---|---|---|---|---|
| `FLAKEPILOT_DATABASE_URL` | API, worker | yes | `fromDatabase` connection string (private network) | Parsed, wired |
| `FLAKEPILOT_HOST` / `FLAKEPILOT_PORT` | both | no | `0.0.0.0` / `10000`, set in the image | Parsed, wired |
| `FLAKEPILOT_LOG_LEVEL` | both | no | `INFO` | Parsed, wired |
| `FLAKEPILOT_EXECUTION_MODE` | both | no | `closed` (also the default) | Parsed; worker runs closed only |
| `FLAKEPILOT_ARTIFACT_DIR` | API | no | `/var/data/artifacts` on the disk | Parsed, wired |
| `FLAKEPILOT_PUBLIC_ORIGIN` | API | no | `https://<domain>` exactly as browsers send `Origin` | Parsed, wired |
| `FLAKEPILOT_GITHUB_CLIENT_ID` / `_CLIENT_SECRET` | API | secret: yes | GitHub App | Parsed, wired (sign-in) |
| `FLAKEPILOT_GITHUB_APP_ID` / `_APP_PRIVATE_KEY` | API | key: yes | GitHub App | Parsed, wired (code-host verification); live minting **NOT VERIFIED** |
| `FLAKEPILOT_SENTRY_DSN` | API | yes | Sentry project DSN (`https://<key>@<host>/<project>`) | Parsed, wired (opt-in reporter) |
| `FLAKEPILOT_AUTH0_DOMAIN` / `_AUDIENCE` | API | no | Auth0 tenant host / API identifier | Parsed; verifier exists, **no route uses it** |
| `FLAKEPILOT_AI_ENDPOINT` / `_AI_MODEL` / `_AI_API_KEY` | API | key: yes | operator model endpoint | Parsed, wired (`LocalEndpointAssist`) |
| `FLAKEPILOT_GEMINI_API_KEY` / `_GEMINI_MODEL` | API | key: yes | Google AI Studio key + `gemma-4-31b-it` or `gemma-4-26b-a4b-it`; not with `FLAKEPILOT_AI_ENDPOINT` | Parsed, wired opt-in (hypothesize stage); live **NOT VERIFIED** |
| `FLAKEPILOT_SERPAPI_API_KEY` | API | yes | SerpApi | Parsed, wired opt-in (research stage); live **NOT VERIFIED** |
| `FLAKEPILOT_BROWSERBASE_API_KEY` / `FLAKEPILOT_BROWSER_ALLOWLIST` | API | key: yes | Browserbase key + comma-separated public host names | Parsed, wired opt-in (browse stage); live **NOT VERIFIED** |
| `FLAKEPILOT_ELEVENLABS_API_KEY` / `_VOICE_ID` (`_MODEL`) | API | key: yes | ElevenLabs | Parsed, wired opt-in (spoken brief route); live **NOT VERIFIED** |
| `FLAKEPILOT_TABPFN_API_KEY` | API (operator shell) | yes | Prior Labs | Parsed, used only by `evaluate-prioritization --with-tabpfn`; live **NOT VERIFIED** |

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

## What this runbook does not claim

- No deploy, account, or spend has happened; nothing above is **VERIFIED** on
  Render.
- `docker build` was **NOT VERIFIED** (no Docker daemon running locally).
- No isolation, encryption-at-rest, or production-readiness claim: Render
  hosting changes none of the evidence levels in
  [PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md).
