# FlakePilot

Evidence-based investigation and verification of recurring test failures. FlakePilot ingests your CI test reports, reconstructs each run's attempt chains, and classifies every failure with a deterministic, versioned rule set — never a model. Since 2026-10-25 the control plane is deployed on Render at **[app.flakepilot.tech](https://app.flakepilot.tech)**; the execution plane stays closed.

*Pre-alpha. Not production-ready.* See [Status and verification](#status-and-verification) for what is verified and what is not.

## Contents

- [What FlakePilot does](#what-flakepilot-does)
- [CI reports, attempts, evidence and classification](#ci-reports-attempts-evidence-and-classification)
- [The investigation workflow](#the-investigation-workflow)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Routes and user workflows](#routes-and-user-workflows)
  - [GitHub authentication](#github-authentication)
  - [CI ingest endpoint](#ci-ingest-endpoint)
  - [Signed-in pages](#signed-in-pages)
  - [Reproductions](#reproductions)
  - [Agent route (Auth0, opt-in)](#agent-route-auth0-opt-in)
- [Security model and limitations](#security-model-and-limitations)
- [Production deployment](#production-deployment)
- [Configuration](#configuration)
  - [GitHub App sign-in setup](#github-app-sign-in-setup)
  - [Hosted providers (ADR-017)](#hosted-providers-adr-017)
- [Operator-only tools](#operator-only-tools)
- [Local development](#local-development)
  - [Prerequisites](#prerequisites)
  - [Quality gates](#quality-gates)
  - [Demo journey](#demo-journey)
- [Status and verification](#status-and-verification)
- [Documentation map](#documentation-map)
- [Contributing](#contributing)
- [Security, licence](#security-licence)

## What FlakePilot does

Flaky tests are treated like incidents, not opinions: every verdict has to come from stored, auditable CI evidence, and every claim FlakePilot makes is labelled with what backs it.

- **One triage surface for flaky and failing tests.** Reports arrive by CI upload or operator import; each stored run keeps its own attempt chain, environment, commit SHA and received-time provenance.
- **Deterministic classification.** A versioned rule set, `ti-classification/1`, decides `flaky`, `deterministic_regression` or a truthful `inconclusive` from stored facts only — no confidence scores, no model output, and no inference the data does not support.
- **Bounded investigation of one run.** When authorised, an investigation walks five provider-backed stages (research, browse, hypothesize, prioritize, brief) inside the audit trail, with each stage honestly `skipped` when its provider is not configured.
- **Fail-closed everywhere.** Missing configuration disables the feature; an unverified fact is stated as unverified; a resource another tenant can see is always `404 not_found`. Nothing in FlakePilot executes repository code. Ever.

## CI reports, attempts, evidence and classification

1. A CI job uploads JUnit XML to `POST /ingest/junit` with a scoped **CI upload token** (issued only by the operator CLI; no HTTP route for token issuance yet). Parsing is synchronous inside the request; the raw report body is stored as a plaintext file under `FLAKEPILOT_ARTIFACT_DIR`.
2. Metadata headers carry the CI identity exactly once each: source, run ID, attempt, job name, commit SHA, ref, environment, started-at and finished-at. The commit SHA is stored as CI reported it; its **revision status stays `unchecked`** — FlakePilot does not verify Git ancestry or what a commit contains.
3. Each test's **attempt chain** (`failed` then `passed`, `failed` then `failed`, …) is reconstructed from the ingested attempts. Maven Surefire rerun-style reports are supported in the same request.
4. The **classification** applies the `ti-classification/1` rules to that evidence and records the verdict with **the evidence it was computed from**, e.g. `flaky` at the commit with its pass/fail attempt counts, or `deterministic_regression` *because a real earlier stored run passed the same test in the same environment* (its `baseline_sha` names that run's commit). A test with insufficient history reports `inconclusive` / `insufficient_history` — never a guess.
5. Every ingested verdict and every refusal is itself data: surfaced on the run view (HTML and JSON), in the operator CLI report, and in the audit trail.

The two session routes (runs listing and run view), the operator `ci-run-report`, and `GET /orgs/{org_id}/runs/{run_id}/classification` all render the same verdict data — one fact stands behind all three shapes.

## The investigation workflow

`POST /orgs/{org_id}/runs/{run_id}/investigation` (session cookie, CSRF same-origin) runs one stored run's bounded investigation. Stages are collected, not one giant prompt:

| Stage | Does | Provider | Absent provider |
|---|---|---|---|
| collect | Assemble the run's evidence from what is stored | — | the run is the collection |
| research | Search the failing tests' names for context | SerpApi | `skipped`, reason `provider_absent` |
| browse | Fetch allow-listed pages as hashed evidence (page text never reaches a model) | Browserbase | `skipped`, reason `provider_absent` |
| hypothesize | A model suggests candidate causes | local endpoint, or hosted Gemma | `skipped`, reason `provider_absent` (org policy disabled: same skip) |
| prioritize | Rank history with the shipped baseline; optional TabPFN candidate | — (TabPFN opt-in) | the baseline is used |
| brief | Render one counts-only spoken summary | ElevenLabs | no audio; 503 `speech_not_configured` |

Response: each stage's status and reason, the research sources (URL, title, received-time), captured page hashes, assist counters (hypothesis text stays in the diagnosis store), the history ranking, the counts-only brief text, and `report_url`. One `diag.investigation.recorded` provenance row records the outcome. The spoken brief (`POST …/investigation/brief`) runs the same investigation again and answers the counts-only brief as audio (`Cache-Control: no-store`; the audio is not stored), with the run view linked in a `Link: … rel="related"` header — the URL is never spoken. Both POSTs sit inside the run view's authorization chain, with `repo.investigate` in place of `repo.view`: an out-of-scope run, an unverified code-host standing or a member without the grant is the same `404 not_found`; a member who can see the repo but lacks `repo.investigate` gets `403 forbidden`; refusals are recorded as provenance too.

AI assist is **off unless the org admin enables it** through the operator command `set-ai-policy` (no HTTP route; ADR-014). Its model output is advisory data: it never changes a deterministic verdict or grants anything. An org that reaches `FLAKEPILOT_AI_DAILY_REQUEST_CAP` (default 500) provider-bound assist requests in a UTC day gets no model call that day (`ai_daily_cap`); ingest is rate-limited per CI token (see [Configuration](#configuration)); both refusals are audited, never silent drops.

## Architecture

Modular monolith: Python 3.12, FastAPI, PostgreSQL. Eleven bounded contexts, each with its own domain, application and infrastructure layers, its own database schema and its own migration directory. Import boundaries are enforced in CI by [import-linter](https://import-linter.readthedocs.io/) contracts (21 of them — see `pyproject.toml`); do not weaken one.

Diagram grounded in the deployed composition. Full lanes and rules: [SYSTEM-ARCHITECTURE](docs/architecture/SYSTEM-ARCHITECTURE.md).

```mermaid
flowchart LR
    subgraph Zone1["Clients (authenticated, input untrusted)"]
        U[/"Browser: GitHub sign-in, runs views, investigations"/]
        CI["CI job: JUnit upload with a token"]
    end
    subgraph Zone2["Control plane (trusted, deployed on Render)"]
        API["API process<br/>FastAPI + React document shell<br/>(import boundaries enforced in CI)"]
        WK["Worker process<br/>durable jobs, outbox relay"]
        DB[("PostgreSQL 16<br/>schemas: iam · ti · diag · orch · exec<br/>aud · repo · int_secrets · ops")]
        DISK[("Plaintext artifact files<br/>(1 GiB disk)")]
    end
    subgraph Zone3["Execution plane (untrusted)"]
        SB["Sandbox port behind ClosedSandbox<br/>default: never starts anything"]
    end
    subgraph Zone4["External providers (opt-in; responses untrusted)"]
        GH["GitHub (sign-in, App verifier)"]
        PR["SerpApi · Browserbase · Gemma ·<br/>ElevenLabs · TabPFN · Sentry"]
        AT["MongoDB Atlas (write-only copy)"]
        TP["Temporal (proof of concept)"]
    end
    U -- "session cookie / CSRF same-origin" --> API
    CI -- "Bearer token + JUnit XML" --> API
    API --> DB
    WK --> DB
    API -- "adapters, only when configured" --> GH
    API -- "adapters, only when configured" --> PR
    API -- "operator write command only" --> AT
    API -- "operator POC command only" --> TP
    API -- "sandbox port (closed in production)" --> SB
    API -- "10 MiB JUnit bodies, plaintext" --> DISK
```

- The **API** process serves HTTP and runs the React document shell. It holds the GitHub App private key, the session secret and the provider keys it was configured with.
- The **worker** process owns durable-job claiming and outbox delivery ([DURABLE-JOBS](docs/operations/DURABLE-JOBS.md)). Leases (default 30 s) recover what a `SIGKILL` interrupts; a `SIGTERM` finishes the claimed batch instead. It holds only the database URL — no provider keys, no session secret, no artifact disk.
- **The sandbox port exists as an interface; production never opens it.** `FLAKEPILOT_EXECUTION_MODE=closed` (default) means no container, no fetched source, no repository code.
- **PostgreSQL is the system of record** (ADR-005). Every context keeps its own schema; migrations are an explicit `alembic upgrade head` step, never startup work.

## Technology stack

| Layer | Choice | Evidence |
|---|---|---|
| Language runtime | Python 3.12 (`.python-version`, uv-managed) | `pyproject.toml`; container image digest-pinned in `Dockerfile` |
| HTTP framework | FastAPI + Uvicorn | `pyproject.toml`; app built in `src/flakepilot/interface/composition_root/` |
| Persistence | PostgreSQL 16; SQLAlchemy async + asyncpg; Alembic migrations | `pyproject.toml`; one schema per context |
| Durable jobs & outbox | PostgreSQL-backed (ADR-006) | worker in `composition_root/worker.py` |
| Served UI | React 19 + TypeScript 5.9, Vite 7 build; no client-side router — Python chooses the document and splices the boot payload | `web/package.json`; built output committed under `src/flakepilot/interface/http/webdist/`, drift-checked in CI |
| Auth adapters | GitHub App (OAuth 2.0 with S256 PKCE), Auth0 M2M client (opt-in) | `src/flakepilot/interface/http/auth.py`; ADR-011 |
| Toolchain | `uv` (lock-gated), ruff (format + check), mypy `strict`, import-linter (21 contracts), pip-licenses, pytest 9 | `pyproject.toml`; [ci.yml](.github/workflows/ci.yml) runs the same code |
| Security & observability | `cryptography` store; Sentry envelope transport (opt-in) | `pyproject.toml`; [SENTRY-OBSERVABILITY-BOUNDARY](docs/operations/SENTRY-OBSERVABILITY-BOUNDARY.md) |

## Routes and user workflows

### GitHub authentication

| Method | Path | Who |
|---|---|---|
| `GET` | `/auth/github/login` | anyone → GitHub (OAuth 2.0, S256 PKCE, `__Host-` cookies) |
| `GET` | `/auth/github/callback` | GitHub → set the session cookie (CSRF state) |
| `POST` | `/auth/logout` | session user (CSRF same-origin) |
| `GET` | `/health/live`, `/health/ready` | process probes — liveness, and readiness with a bounded database check |

Sign-in authenticates; **grants come from FlakePilot's own IAM records**, never from a GitHub org or repository relationship (ADR-011). Without the GitHub App credentials configured the session routes answer `503 auth_not_configured`; a half-configured credential pair stops startup instead (fail closed).

### CI ingest endpoint

`POST /ingest/junit` with `Authorization: Bearer fpi_…`. Token scope is one (org, repo) pair. Body 10 MiB at most. 201 accepted; 200 duplicate with the idempotency key and content; 409 `idempotency_conflict` for the same key with other content. Answers are rate-limited per token (see [Configuration](#configuration)). Details: ADR-013.

### Signed-in pages

All served by the Python backend, which authorizes the request and then splices a boot payload into the React document shell (`shell.py`). Scripts, styles and fonts are same-origin hashed assets; the CSP is `default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; frame-ancestors 'none'`.

| Method | Path | What |
|---|---|---|
| `GET` | `/orgs/{org_id}/repos/{repo_id}/runs` | one repo's stored runs, newest first (default page 50, hard bound 200) |
| `GET` | `/orgs/{org_id}/runs/{run_id}` | one read-only run view: attempt chains and per-test `ti-classification/1` verdicts with evidence |
| `GET` | `/orgs/{org_id}/runs/{run_id}/classification` | the same verdicts as a JSON document |
| `POST` | `/orgs/{org_id}/runs/{run_id}/investigation` | the bounded investigation (above) |
| `POST` | `/orgs/{org_id}/runs/{run_id}/investigation/brief` | the counts-only spoken brief |

The public root (`GET /`) is server-rendered React, no session; session views require a signed-in member with the exact `repo.view` grant, a verified code-host standing on that exact (org, repo) pair, and an org the member belongs to; each is a `404 not_found` otherwise, and a `401 unauthenticated` with no valid cookie.

### Reproductions

`POST /orgs/{org_id}/reproductions` schedules a bounded reproduction of one selector at one commit (body: `repo_id`, `selector`, `sha` — 40-hex). **Closed mode (default) stores `inconclusive` / `execution_closed`, fetches nothing, enqueues nothing and starts nothing.** Repeating a (repo, selector, sha) tuple returns the same execution ID.

- `GET /orgs/{org_id}/reproductions/{execution_id}` — state, outcome, artifact names (no bytes).
- `GET /orgs/{org_id}/reproductions/{execution_id}/artifacts/{kind}` — artifact bytes (`result` or `screenshot`), only after the same authorization chain and only for keys owned by that execution.

### Agent route (Auth0, opt-in)

`POST /agent/orgs/{org_id}/runs/{run_id}/investigation` runs the same bounded investigation for a machine-to-machine agent:

- is **mounted only when both** `FLAKEPILOT_AUTH0_DOMAIN` **and** `FLAKEPILOT_AUTH0_AUDIENCE` **are set** (unset: unknown route);
- answers a bearer token verified against the tenant; a session cookie buys nothing here — the two channels never mix;
- requires the token's subject (`<client_id>@clients`) to be currently bound by an org admin (`bind-agent`), with the admin's live grants and verified code-host standing re-derived on every request — token possession alone is not authority;
- answers `401 unauthenticated` for a tampered token and `404 not_found` once the binding is revoked.

Details and every answer code: [AUTH0-AGENT-ROUTE](docs/operations/AUTH0-AGENT-ROUTE.md).

## Security model and limitations

> The control plane **does not run repository code.** A dedicated execution host is a different trust boundary: not a docker container on a laptop, and never production isolation evidence.

- **Two planes, one boundary control.** The control plane never executes assistant-produced code and never fetches repository source: execution is opt-in (`FLAKEPILOT_EXECUTION_MODE=evaluation`) and even then is not a production sandbox. Every composition wires `ClosedSandbox`; no container runtime is installed in the image (ADR-004).
- **HMAC possession alone is not permission**, and no request frame can choose a host path, program or container id: the runtime inventory comes only from the (closed) execution host contract ([EXECUTION-HOST](docs/operations/EXECUTION-HOST.md)).
- **Least-privilege tokens.** GitHub authorization stays server-side: `contents:read` on one repository, minted per request. The single exception is the operator's `backfill-ci-history` token (`actions:read` instead of `contents:read`, on the same one registered repository). No token carries both, and none carries a write permission.
- **Bounded artifacts.** Raw reports land in one dedicated directory — existing, absolute, owned by the service user, not group/world-writable, outside the repository — and artifact keys never leave the process. **They are plaintext and never cleaned up** (no retention job is registered, and retention as a product decision is recorded, not decided).
- **Non-disclosing refusals.** A resource the caller cannot see is the same `404 not_found` as a missing one; validation never echoes a secret value; a foreign org learns nothing. Every denial, every rate-limit refusal and every provider call is in the audit trail.
- **Evidence, not vibes.** Classifications may be `inconclusive` on purpose (`insufficient_history`); reproduction in closed mode is `inconclusive` / `execution_closed`; revisions stay `unchecked`; every ambiguous outcome is stated as such instead of being given a confident answer.
- **TLS and request limits are your job** when you self-host. Run behind a TLS-terminating reverse proxy that also enforces request size and time limits.

## Production deployment

Render Blueprint (`render.yaml`, deployment rationale in [RENDER-DEPLOYMENT-ARCHITECTURE](docs/operations/RENDER-DEPLOYMENT-ARCHITECTURE.md)), live at `https://app.flakepilot.tech` since 2026-10-25 (singapore region):

| Service | Responsibility | Holds |
|---|---|---|
| `flakepilot-api` (web) | HTTP, sign-in, pages, ingest, investigations | database URL, session secret, GitHub App key, provider keys it needs; 1 GiB artifacts disk |
| `flakepilot-worker` | durable jobs, outbox delivery | **database URL only** |
| `flakepilot-db` (PostgreSQL 16) | system of record | private network only (`ipAllowList: []`) |

- Migrations are a separate `preDeployCommand` step: a failed migration fails the deploy — never startup work. Deploys are gated on CI checks passing (`autoDeployTrigger: checksPass`).
- Execution is **closed** on every service (no container runtime, no repository code). Not production-ready: no isolation, no encryption at rest, no quotas.
- The Auth0 agent route is mounted and configured on the deployment; an end-to-end exercised production path through it with a live token is not yet recorded (see [Status and verification](#status-and-verification)).
- Runbook, deployed-state boundaries and the exact render commands: [DEPLOYMENT-RUNBOOK](docs/operations/DEPLOYMENT-RUNBOOK.md#deployed-state-2026-10-25).

## Configuration

Settings come from environment variables only (see [`.env.example`](.env.example)). Empty means unset. Invalid values stop startup with a message that names the variable but never echoes a value.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FLAKEPILOT_DATABASE_URL` | yes | — | PostgreSQL URL, `postgresql://USER:PASSWORD@HOST:5432/DBNAME` |
| `FLAKEPILOT_LOG_LEVEL` | no | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` |
| `FLAKEPILOT_HOST` | no | `127.0.0.1` | Bind address (an IP address) |
| `FLAKEPILOT_PORT` | no | `8000` | Bind port |
| `FLAKEPILOT_PUBLIC_ORIGIN` | for sign-in | — | The public `https://host[:port]`. Used for the OAuth callback and the CSRF origin check |
| `FLAKEPILOT_GITHUB_CLIENT_ID` / `_SECRET` | for sign-in | — | GitHub App client credentials |
| `FLAKEPILOT_GITHUB_APP_ID` / `_PRIVATE_KEY` | for session views | — | GitHub App id and unencrypted RSA key, together, for the code-host access verifier. Unset: session views answer `404` (fail closed) |
| `FLAKEPILOT_ALLOW_INSECURE_COOKIES` | no | `false` | `true` only for local plain-HTTP development. Never in production |
| `FLAKEPILOT_SECRET_KEY` | no | — | 32 random bytes, base64, validated if set — **not used yet** |
| `FLAKEPILOT_ARTIFACT_DIR` | for ingestion | — | Existing absolute directory for raw reports (owned by the service user, not group/world-writable, outside the repository). Unset: ingest answers `503 ingestion_not_configured` |
| `FLAKEPILOT_EXECUTION_MODE` | no | `closed` | `closed` never starts a container. `evaluation` is an explicit opt-in, not production isolation |
| `FLAKEPILOT_EXECUTION_RUNTIME` | no | `docker` | `docker` or `podman`, evaluation mode only |
| `FLAKEPILOT_EXECUTION_IMAGE` | for evaluation | — | Digest-pinned image, required when mode is `evaluation` |
| `FLAKEPILOT_EXECUTION_WALL_SECONDS` | no | `30` | Sandbox wall clock, 5–120 |
| `FLAKEPILOT_EXECUTION_MEMORY_MB` | no | `512` | Container memory limit, 64–2048 |
| `FLAKEPILOT_EXECUTION_PIDS` | no | `64` | Container process limit, 8–256 |
| `FLAKEPILOT_WORKER_POLL_SECONDS` | no | `2` | Worker poll interval, 1–300 (API process ignores it) |
| `FLAKEPILOT_WORKER_BATCH_SIZE` | no | `10` | Jobs and outbox rows per tick, 1–100 |
| `FLAKEPILOT_WORKER_LEASE_SECONDS` | no | `30` | Job lease length, 5–3600 |
| `FLAKEPILOT_INGEST_RATE_LIMIT` / `_WINDOW_SECONDS` | no | 120 / 3600 | Ingest cost control per CI token; over the limit: `429 rate_limited`, audited |
| `FLAKEPILOT_AI_ENDPOINT` / `_MODEL` / `_API_KEY` | no | — | Operator-run local inference server for advisory assist; both endpoint *and* model are required together. Refused beside Gemma |
| `FLAKEPILOT_AI_DAILY_REQUEST_CAP` | no | `500` | Provider-bound assist requests per org per UTC day while its policy is enabled |
| `FLAKEPILOT_SENTRY_DSN` | no | — | API process only, allow-listed ERROR events ([boundary](docs/operations/SENTRY-OBSERVABILITY-BOUNDARY.md)) |
| `FLAKEPILOT_AUTH0_DOMAIN` / `_AUDIENCE` | no | — | Together: mounts the agent investigation route (see [Agent route](#agent-route-auth0-opt-in)) |
| `FLAKEPILOT_GEMINI_API_KEY` / `_MODEL` | no | — | Hosted Gemma for assist, together (`gemma-4-31b-it` or `gemma-4-26b-a4b-it`) |
| `FLAKEPILOT_SERPAPI_API_KEY` | no | — | SerpApi research stage |
| `FLAKEPILOT_BROWSERBASE_API_KEY` / `_ALLOWLIST` | no | — | Browserbase browse stage, together; exact public host names |
| `FLAKEPILOT_ELEVENLABS_API_KEY` / `_VOICE_ID` | no | — | ElevenLabs spoken brief, together |
| `FLAKEPILOT_ELEVENLABS_MODEL` | no | `eleven_flash_v2_5` | Or `eleven_multilingual_v2` |
| `FLAKEPILOT_TABPFN_API_KEY` | no | — | `evaluate-prioritization --with-tabpfn` only |
| `FLAKEPILOT_ATLAS_URI` / `_DATABASE` | no | — | Together: write-only MongoDB Atlas copy for `export-reports-atlas`; PostgreSQL stays the system of record and FlakePilot never reads Atlas back. The URI must keep TLS on |
| `FLAKEPILOT_TEMPORAL_ADDRESS` / `_NAMESPACE` / `_API_KEY` | no | — | Temporal POC wiring, together; API key turns TLS on. The default worker and the API process never connect |
| `FLAKEPILOT_GITHUB_CONTENTS_TOKEN` | no | — | Optional bearer for GitHub contents reads. Empty: revisions stay `unchecked` |
| `FLAKEPILOT_TEST_DATABASE_URL` | tests | — | A throwaway PostgreSQL URL for `integration` tests (needs `CREATEDB`). Never a real database |

### GitHub App sign-in setup

1. Create a GitHub App.
2. Set its callback URL to `{FLAKEPILOT_PUBLIC_ORIGIN}/auth/github/callback`.
3. Set `FLAKEPILOT_PUBLIC_ORIGIN`, `FLAKEPILOT_GITHUB_CLIENT_ID` and `FLAKEPILOT_GITHUB_CLIENT_SECRET`.

Without all three, sign-in is off (`503 auth_not_configured`); half-set pairs stop startup. Sign-in alone grants nothing: an operator bootstraps the org, registers repositories and issues grants through the CLI ([Operator-only tools](#operator-only-tools)); GitHub's own permissions never confer access.

### Hosted providers (ADR-017)

Every hosted provider is off with **no network contact** unless its settings are complete; a half-set pair refuses startup without echoing a key. They are read by the API process and the operator commands only — never give them to the worker. "Configured" is not "working": the process never health-checks a provider. Which adapters have been run against the live service, and how, is recorded in [SPONSOR-IMPLEMENTATION-MAP](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md).

| Provider | Variables | Used by | What leaves the process |
|---|---|---|---|
| Gemma (Gemini API) | `FLAKEPILOT_GEMINI_API_KEY`, `FLAKEPILOT_GEMINI_MODEL` | hypothesize stage, after the org enables AI policy | stage instructions and the run's evidence parts (classname, name, id, attempt outcomes) |
| SerpApi | `FLAKEPILOT_SERPAPI_API_KEY` | research stage | the failing tests' names as the search query |
| Browserbase | `FLAKEPILOT_BROWSERBASE_API_KEY`, `FLAKEPILOT_BROWSER_ALLOWLIST` | browse stage | allow-listed URLs; pages come back as hashed evidence |
| ElevenLabs | `FLAKEPILOT_ELEVENLABS_API_KEY`, `FLAKEPILOT_ELEVENLABS_VOICE_ID` | `POST …/investigation/brief` only | the counts-only brief text |
| TabPFN | `FLAKEPILOT_TABPFN_API_KEY` | `evaluate-prioritization --with-tabpfn` only | numeric feature rows and 0/1 labels |
| Sentry | `FLAKEPILOT_SENTRY_DSN` | API error reporting | allow-listed event fields only |
| MongoDB Atlas | `FLAKEPILOT_ATLAS_URI`, `FLAKEPILOT_ATLAS_DATABASE` | `export-reports-atlas` only | classified report summaries: IDs, counts, enums, commit SHA, environment, attempt outcomes, verdicts + evidence |
| Temporal | `FLAKEPILOT_TEMPORAL_ADDRESS`, `FLAKEPILOT_TEMPORAL_NAMESPACE` (plus `FLAKEPILOT_TEMPORAL_API_KEY` for Temporal Cloud) | `temporal-*` commands only | workflow input/result: investigation key, org, run, member IDs, stopping condition, stage count |

## Operator-only tools

These subcommands of `python -m flakepilot.interface.composition_root` are **operator trust**: no HTTP route exists for any of them, each is audited, and ordinary users never reach them. Grant administration deliberately has no browser UI (ADR-015).

| Command | Does |
|---|---|
| `bootstrap-org` | create the org and its first admin (prints the org ID only) |
| `register-repo`, `add-member`, `grant`, `revoke`, `show-access` | register a repo in the org, add the member, change grants; IAM membership changes revoke live sessions |
| `issue-ci-upload-token` | issue a CI upload token to one (org, repo) pair; the raw token is shown once |
| `set-ai-policy` | enable or disable the org's assist policy (as that org admin) |
| `bind-agent` / `unbind-agent` | bind or revoke an Auth0 M-to-M subject's delegated investigation scope (at most 8 hours; stores the subject's SHA-256) |
| `list-runs` | the stored-runs listing as data (read-only) |
| `ci-run-report` | one run's classified report as JSON or self-contained HTML (offline) |
| `backfill-ci-history` | import a repo's GitHub Actions JUnit artifacts with original CI completion time (needs `FLAKEPILOT_GITHUB_APP_ID`/`_PRIVATE_KEY` and `actions:read`) |
| `export-reports-atlas` | upsert a pair's newest classified reports to Atlas (idempotent; first failure stops the batch) |
| `provider-status`, `reliability-trend`, `evaluate-prioritization` | read-only analytics (JSON on stdout) |
| `backlog-status` | undelivered outbox rows and job-queue backlog counts (paste-safe) |
| `verify-github-app` | read-only staging diagnostic for the GitHub App and the code-host verifier; a no-credentials fail-closed probe always runs |
| `verify-run`, `verify-fetch` | run one target command as an evidenced subprocess — the real exit code is recorded and read back. `--b64-exec` passes the target as base64 for quote-free operation |
| `temporal-worker`, `temporal-run-investigation`, `temporal-cancel-investigation` | the Temporal proof of concept (not the default worker); FlakePilot's advisory lock and audit rows stay authoritative |

The worker is not a user feature either; it is the second process:

```sh
uv run python -m flakepilot.interface.composition_root worker
```

## Local development

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs and uses its own CPython 3.12 — see `.python-version`)
- PostgreSQL 16 (local, created manually with `createdb`)
- Node 22 and npm, only to rebuild the web documents after changing `web/`

```sh
uv sync --locked
```

Migrations are explicit, then run the API and the worker in two terminals:

```sh
uv run alembic upgrade head                                # never run at startup
uv run python -m flakepilot.interface.composition_root     # the API
uv run python -m flakepilot.interface.composition_root worker
```

Unreachable PostgreSQL exits with code 3 at startup, so wait on a health check or enable a restart policy. Local artifacts need an existing, absolute, `700` directory outside the repository:

```sh
mkdir -m 700 /tmp/flakepilot-demo-artifacts
export FLAKEPILOT_ARTIFACT_DIR=/tmp/flakepilot-demo-artifacts
export FLAKEPILOT_DATABASE_URL='postgresql://postgres@localhost:5432/flakepilot_local'
```

Web rebuild — output is committed, CI asserts no drift:

```sh
cd web && npm ci && npm run build    # tsc --noEmit && vite build; writes ../src/flakepilot/interface/http/webdist/
```

### Quality gates

The same commands run in CI ([ci.yml](.github/workflows/ci.yml)); toolchain rationale is ADR-010.

```sh
uv lock --check
uv sync --locked
uv run pytest                      # CI adds --junitxml=reports/junit.xml and uploads it
uv run ruff format --check
uv run ruff check
uv run mypy
uv run lint-imports
uv run pip-licenses --ignore-packages pathspec:1.1.1
UV_PROJECT_ENVIRONMENT=.venv-runtime uv sync --locked --no-dev
uv run pip-licenses --python .venv-runtime/bin/python
cd web && npm ci && npm run build  # the typecheck-and-build gate; also checks the webdist tree
```

- `pip-licenses` checks installed packages only; shipped container images need the separate runtime-gate command above (no exceptions). The one exception in the dev gate — `pathspec` 1.1.1, MPL-2.0, dev-only — is recorded next to `[tool.pip-licenses]` in `pyproject.toml`.
- Tests marked `integration` need `FLAKEPILOT_TEST_DATABASE_URL` and create/drop throwaway databases per test or module, so the role needs `CREATEDB`. **Never point it at a real database.** A missing URL only fails in CI, where PostgreSQL is provided.
- On a host that refuses `CREATE DATABASE`, set `FLAKEPILOT_TEST_SHARED_DATABASE=1`: tests then run against the URL itself with fail-closed guards and a schema-scoped `TRUNCATE` (`tests/integration/test_shared_database_postgres.py` pins this).

### Demo journey

One command drives the operator journey end to end — bootstrap, token issue, two fixture uploads, discovery, classified report, evidence narrative — into `flakepilot-demo-output/`:

```sh
createdb flakepilot_demo                                     # once
export FLAKEPILOT_DATABASE_URL='postgresql://postgres@localhost:5432/flakepilot_demo'
ops/demo/run-demo.sh                                         # or: sh ops/demo/run-demo.sh
```

It states plainly what the demo is and is not: hand-written fixtures in `ops/demo/`, no repository code ever executed, `FLAKEPILOT_EXECUTION_MODE=closed` throughout, `baseline_sha` evidence recorded without Git-ancestry claims, and every revision stays `unchecked`.

## Status and verification

**Pre-alpha. Not production-ready.** What is live on the deployment as of 2026-10-25: GitHub sign-in, the runs listing and run view (the session-view code-host verifier), the investigation route (SerpApi + Gemma after `set-ai-policy` + ElevenLabs spoken brief), and the control plane's own closed execution posture. Recorded verification (2026-10-24/25 developer runs): Gemma, SerpApi, ElevenLabs, Sentry and TabPFN (synthetic rows) adapters were exercised against their real services; one real Auth0 token traversed the agent route (**in-process test** with a stand-in code-host standing); a real backfilled run was imported; Atlas export ran live and was cleaned up; Temporal POC ran against a local dev server, not Temporal Cloud.

Honest, open gaps:

- **The agent route has not been exercised on the deployment with a live Auth0 token** (mounted and tamper-rejecting there, but no bound-agent end-to-end yet); ongoing work lives in [SPONSOR-IMPLEMENTATION-MAP](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md).
- **Atlas export on the deployment is not yet evidenced to completion** — the export path is implemented and the database side is clean; container-side receipts are still being gathered.
- **No quotas for ordinary use, no artifact retention, no encryption at rest** — and the artifacts directory has no automatic cleanup.
- **Isolation evidence is not yet reviewed on real isolated infrastructure** (`IsolationChecks` stay false); no environment variable can mark missing checks as passed, and execution stays closed until the evidence exists.
- **Browserbase has never been run live**; no key exists in this repository.
- **Container isolation is not established.** `per_run_microvm` evidence would require a dedicated host; today there is none ([FIRECRACKER-FEASIBILITY-ASSESSMENT](docs/operations/FIRECRACKER-FEASIBILITY-ASSESSMENT.md) and [FIRECRACKER-THREAT-MODEL](docs/operations/FIRECRACKER-THREAT-MODEL.md) are read-only analyses, not adoption).
- **The release register** ([RELEASE-CANDIDATE](docs/operations/RELEASE-CANDIDATE.md), [RELEASE-READINESS-REGISTER](docs/operations/RELEASE-READINESS-REGISTER.md)) keeps the local-only release-candidate boundary and upgrade criteria; today's deployment is not that release.

## Documentation map

| Topic | Document |
|---|---|
| Product definition, roles and permission boundaries | [docs/product/PRODUCT-SCOPE.md](docs/product/PRODUCT-SCOPE.md) |
| System architecture, workflows, ports, SLOs | [docs/architecture/](docs/architecture/) |
| Decision record index (ADR-001 … ADR-017) | [docs/adr/README.md](docs/adr/README.md) |
| Deployment infrastructure and runbooks | [docs/operations/DEPLOYMENT-RUNBOOK.md](docs/operations/DEPLOYMENT-RUNBOOK.md), [docs/operations/RENDER-DEPLOYMENT-ARCHITECTURE.md](docs/operations/RENDER-DEPLOYMENT-ARCHITECTURE.md), [docs/operations/EXECUTION-HOST.md](docs/operations/EXECUTION-HOST.md) |
| Durable jobs and the outbox | [docs/operations/DURABLE-JOBS.md](docs/operations/DURABLE-JOBS.md) |
| Execution (browser reproduction) boundaries | [docs/operations/EXECUTION.md](docs/operations/EXECUTION.md) |
| Sponsor integration statuses | [docs/operations/SPONSOR-IMPLEMENTATION-MAP.md](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md), [docs/operations/SPONSOR-INTEGRATION-MATRIX.md](docs/operations/SPONSOR-INTEGRATION-MATRIX.md) |
| Release boundary and readiness | [docs/operations/RELEASE-CANDIDATE.md](docs/operations/RELEASE-CANDIDATE.md), [docs/operations/RELEASE-READINESS-REGISTER.md](docs/operations/RELEASE-READINESS-REGISTER.md) |

## Contributing

Work happens on `main`. Read [AGENTS.md](AGENTS.md) first — it is the canonical guide to the repository's shape, the fail-closed inventory, and the Git and quality-gate conventions (never weaken an import-linter contract, run every gate before committing, keep the committed web build in sync with `web/`). Changes that alter behaviour should reference the ADR they follow, or open a new one when the decision is new. Keep changes small, factual and tested.

## Security, licence

Security policy and private disclosure: [SECURITY.md](SECURITY.md). Code under the [Apache-2.0](LICENSE) licence; the bundled web fonts are under SIL OFL 1.1 (`web/OFL-1.1.txt`).
