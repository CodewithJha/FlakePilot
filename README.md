# FlakePilot

Evidence-based investigation and verification of recurring test failures. See [PRODUCT-SCOPE](docs/product/PRODUCT-SCOPE.md).

## Status

**Pre-alpha. Not production-ready.** The backend foundations exist: sign-in, org bootstrap, CI upload tokens, JUnit ingestion and a failure classifier. The release statement, limitations, and upgrade criteria: [RELEASE-CANDIDATE](docs/operations/RELEASE-CANDIDATE.md) (closed as a local-only release candidate; the control plane has since been deployed on Render — see the update note there and [DEPLOYMENT-RUNBOOK](docs/operations/DEPLOYMENT-RUNBOOK.md)). Known gaps:

- **No UI beyond two read-only pages.** The session-user runs listing (`GET
  /orgs/{org_id}/repos/{repo_id}/runs`) and run view
  (`GET /orgs/{org_id}/runs/{run_id}`) are the only server-rendered HTML. They
  are read-only, require a signed-in member with `repo.view` on the exact
  repository, and are answered 503 `auth_not_configured` when sign-in is not
  configured, like the other session routes.
- **CI upload tokens are issued only by the operator CLI** (`issue-ci-upload-token`, see the demo journey below). No HTTP route exists for them yet.
- **Classification verdicts are also data.** Besides the two session run views and the operator CLI's `ci-run-report`, `GET /orgs/{org_id}/runs/{run_id}/classification` returns a signed-in member's run's `ti-classification/1` verdicts (with their evidence) as one JSON document — same authorization chain as the HTML view, same non-disclosing denials.
- **Parsing is synchronous.** Reports are parsed inside the upload request. A separate worker process runs durable jobs and delivers outbox rows to the audit log. No retention or stage job is registered. See [DURABLE-JOBS](docs/operations/DURABLE-JOBS.md).
- **Cost controls (2026-10-25).** Ingest is rate-limited per CI token: more than `FLAKEPILOT_INGEST_RATE_LIMIT` (default 120) processed uploads in `FLAKEPILOT_INGEST_RATE_WINDOW_SECONDS` (default 3600) answers 429 `rate_limited`, and every refusal is a recorded, audited `rate_limited` rejection — never a silent drop. An org with an enabled AI policy that reaches `FLAKEPILOT_AI_DAILY_REQUEST_CAP` (default 500) provider-bound assist requests in a UTC day gets no model call that day (`ai_daily_cap`; the evidence path and policy are unchanged). Both bounds are set in [`.env.example`](.env.example).
- **AI assist is off unless an org admin enables it**, through the operator command `set-ai-policy` (no HTTP route). It runs only inside the investigation route below, through a model the operator configured (a local endpoint or hosted Gemma); with none configured the process wires `NullAiAssist` and assist answers `unavailable`. See ADR-014.
- **Hosted providers are opt-in; five are live-verified locally, and Gemma, SerpApi and ElevenLabs also run on the deployment.** Gemma (Gemini API), SerpApi, Browserbase, ElevenLabs and TabPFN adapters are wired only when their keys are set (see [Hosted providers](#hosted-providers-adr-017)); no key exists in this repository. The Gemma, SerpApi, ElevenLabs and Sentry adapters were run against the real services from a developer machine on 2026-10-24, and TabPFN (synthetic rows only; no evaluation on real history) on 2026-10-25; Browserbase has not been. On 2026-10-25 a real Auth0 token went through the opt-in agent investigation route (in-process; the code-host standing was a stand-in), one real investigation produced a Gemma hypothesis, the MongoDB Atlas export ran live (adapter round trip, and `export-reports-atlas` on one real backfilled run, both cleaned up), `backfill-ci-history` imported FlakePilot's own CI run, and the Temporal proof of concept ran against a real local Temporal dev server, not Temporal Cloud ([SPONSOR-IMPLEMENTATION-MAP](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md#live-verification-record-2026-10-24)).
- **Artifacts are not encrypted at rest, and never cleaned up.** Raw reports are plain files under `FLAKEPILOT_ARTIFACT_DIR`, and there is no retention job.
- **Browser reproduction fails closed.** The default execution mode starts no container and runs no repository code. Evaluation mode is an explicit opt-in, builds a `docker` or `podman` argument list, and is not a microVM and not a production sandbox. No container was run in CI. See [EXECUTION](docs/operations/EXECUTION.md).
- **The control plane is deployed on Render; execution is not.** Since 2026-10-25 the API, worker and PostgreSQL 16 from `render.yaml` run in Render's singapore region at `https://app.flakepilot.tech` (sign-in starts at `/auth/github/login`). Live there: GitHub sign-in and the run view (the session-view code-host verifier), the investigation route with SerpApi research and, after `set-ai-policy`, a Gemma hypothesis, and the spoken brief. The worker holds only the database URL; no repository code runs. Not production-ready: no isolation, no encryption at rest, no quotas. See [DEPLOYMENT-RUNBOOK](docs/operations/DEPLOYMENT-RUNBOOK.md).
- **TLS and request limits are your job.** Run it only behind a TLS-terminating reverse proxy that also enforces request size and time limits.

## Architecture

FlakePilot is a modular monolith: Python, FastAPI and PostgreSQL. It is split into bounded contexts, each with its own domain, application and infrastructure layers, its own database schema, and import boundaries enforced in CI.

- **What it does and why:** [PRODUCT-SCOPE](docs/product/PRODUCT-SCOPE.md).
- **System design, contexts and dependency rules:** [docs/architecture/](docs/architecture/).
- **Decisions:** [docs/adr/README.md](docs/adr/README.md).

## Local setup

Requires [uv](https://docs.astral.sh/uv/). uv uses its managed CPython 3.12 (`.python-version`), never the host default.

```sh
uv sync --locked
```

## MVP demo journey (operator-led, end to end)

The shortest live journey today is CI ingestion (Workflow B), driven by the
operator CLI. Sign-in needs GitHub App credentials and is out of scope
for this demo; without them the session routes answer 503 `auth_not_configured`,
so the demo uses the operator CLI paths that do not need them. Everything below
is truthful about what it is and is not: the demo fixture is hand-written, no
repository code is ever executed, and `FLAKEPILOT_EXECUTION_MODE` stays `closed`.

One command runs the whole journey (`createdb flakepilot_demo` once beforehand).
It writes its HTML report, the discovery listing, the JSON report and the
evidence narrative into `flakepilot-demo-output/` (overridable; see the script
header):

```sh
ops/demo/run-demo.sh     # or sh ops/demo/run-demo.sh
```

Step by step:

1. **Create the database** (PostgreSQL must be running locally):

   ```sh
   createdb flakepilot_demo
   export FLAKEPILOT_DATABASE_URL='postgresql://postgres@localhost:5432/flakepilot_demo'
   ```

2. **Start the stack** with `uv sync --locked`, then `uv run alembic upgrade head`
   (migrations are an explicit step), then the API and the worker as documented
   under "Run the API" below. Ingestion also needs an artifacts directory
   (existing, absolute, outside the repository; plaintext storage, no retention):

   ```sh
   mkdir -m 700 /tmp/flakepilot-demo-artifacts
   export FLAKEPILOT_ARTIFACT_DIR=/tmp/flakepilot-demo-artifacts
   ```

3. **Bootstrap an org** with the existing operator command:

   ```sh
   uv run python -m flakepilot.interface.composition_root bootstrap-org \
       --name DemoOrg --admin-github-id 424242 --admin-login demo-admin
   ```

   It prints the new org ID. `424242` is local demo data, not a real account.

4. **Issue a CI upload token** (new in this MVP; previously there was no HTTP
   route or CLI for this):

   ```sh
   REPO_ID=$(uv run python -c "import uuid; print(uuid.uuid4())")
   uv run python -m flakepilot.interface.composition_root issue-ci-upload-token \
       --org-id "$ORG_ID" --repo-id "$REPO_ID"
   ```

   The org ID must be the one from step 3, and the repo ID is a demo-scoped
   UUID (nothing is registered at a code host). Only the token's SHA-256 is
   stored; the raw token is shown once. The token may upload reports to this
   one (org, repo) pair and nothing else.

5. **Upload the demo fixtures** (hand-written JUnit files in Maven Surefire
   rerun style, committed under [ops/demo](ops/demo/)): the passing baseline on
   an earlier demo commit, then the flaky/failing run. Both are fixtures —
   clearly labelled as such; no repository code produced or ran them.

   ```sh
   curl -sS -X POST localhost:8000/ingest/junit \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/xml" \
     -H "X-FlakePilot-CI-Source: demo-fixture" -H "X-FlakePilot-CI-Run-Id: demo-run-baseline" \
     -H "X-FlakePilot-CI-Attempt: 1" -H "X-FlakePilot-Job-Name: demo.test-suite" \
     -H "X-FlakePilot-Commit-Sha: 2424242424242424242424242424242424242424" \
     -H "X-FlakePilot-Ref: refs/heads/demo" -H "X-FlakePilot-Environment: demo.local" \
     -H "X-FlakePilot-Started-At: 2026-10-23T12:00:00+00:00" \
     -H "X-FlakePilot-Finished-At: 2026-10-23T12:05:00+00:00" \
     --data-binary @ops/demo/fixture-junit-baseline.xml

   curl -sS -X POST localhost:8000/ingest/junit \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/xml" \
     -H "X-FlakePilot-CI-Source: demo-fixture" -H "X-FlakePilot-CI-Run-Id: demo-run-1" \
     -H "X-FlakePilot-CI-Attempt: 1" -H "X-FlakePilot-Job-Name: demo.test-suite" \
     -H "X-FlakePilot-Commit-Sha: 000102030405060708090a0b0c0d0e0f10111213" \
     -H "X-FlakePilot-Ref: refs/heads/demo" -H "X-FlakePilot-Environment: demo.local" \
     -H "X-FlakePilot-Started-At: 2026-10-23T12:10:00+00:00" \
     -H "X-FlakePilot-Finished-At: 2026-10-23T12:15:00+00:00" \
     --data-binary @ops/demo/fixture-junit.xml
   ```

   Each `201` response carries the upload ID, run ID, and the summary counts.

6. **Discover the stored runs** with `list-runs` (the shipped read-only discovery
   command). The listing is newest first and bounded — the run IDs you can then
   use with `ci-run-report` and the session run views come from this actual
   output, never from memory:

   ```sh
   uv run python -m flakepilot.interface.composition_root list-runs \
       --org-id "$ORG_ID" --repo-id "$REPO_ID" --limit 2 --format json
   ```

   A repository with no stored runs prints `no stored runs for this (org,
   repo) pair yet` instead of an error; a foreign org's listing is empty.

7. **Render the selected run** — taking `RUN_ID` from the listing above — as a
   read-only JSON report, then as a self-contained HTML report:

   ```sh
   uv run python -m flakepilot.interface.composition_root ci-run-report \
       --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" \
       --format json
   uv run python -m flakepilot.interface.composition_root ci-run-report \
       --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" \
       --format html --out demo-run.html && open demo-run.html
   ```

   The report shows the run's summary counts, the upload metadata, each
   test's attempt chain, and each test's **classification** — the
   deterministic, versioned rule set `ti-classification/1` applied to the
   stored CI evidence (no confidence scores, no model output). On the demo
   data: the flaky test is classified `flaky` at the failing commit with the
   passing and failing attempt counts as evidence; the failing test is
   classified `deterministic_regression` **because a real earlier stored run
   passed it in the same environment** (`baseline_sha` names that run's
   commit); a test that only passed reports a truthful `inconclusive` /
   `insufficient_history`. The commit SHA is what CI reported, the revision
   status stays `unchecked`, and the raw report body is stored as a plaintext
   file. `--format json` is available for scripts.

8. **Explain the evidence.** The script (`ops/demo/run-demo.sh`, one command
   for the whole journey) prints an evidence narrative after the reports,
   saved to `flakepilot-demo-output/evidence-narrative.txt` together with the
   discovery listing (`runs.json`) and the JSON report. It cites only what the
   rule set recorded — stored run IDs from the actual `list-runs` output,
   attempt chains (`failed` then `passed`, or `failed` then `failed`), the
   environment, and the rule version — and states plainly what the demo is
   not: the fixtures are hand-written synthetic data, no repository code ever
   ran, baseline-before-current ordering is a demo upload choice and **not**
   proof of Git ancestry, and every run's revision status stays `unchecked`.

Known limits of this journey: there is no interactive UI beyond the session-user
run view (a signed-in reader needs GitHub App credentials, which no demo
provides — see "GitHub App sign-in"); classification is surfaced in the operator
report and the session-user run page only (the rule set version and evidence
are rendered, so every verdict is traceable to the stored facts it was computed
from); reproduction runs remain fail-closed
(`inconclusive` / `execution_closed`, no source fetched); artifacts are
plaintext and retention deletes nothing; the demo org/token are created by
you, on your machine, out of the process's control plane.

## Configuration

Settings come from environment variables only (see [`.env.example`](.env.example)). Empty means unset. An invalid value stops startup with a message that names the variable but never shows its value.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FLAKEPILOT_DATABASE_URL` | yes | — | PostgreSQL URL, `postgresql://USER:PASSWORD@HOST:5432/DBNAME`. Never logged |
| `FLAKEPILOT_LOG_LEVEL` | no | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` |
| `FLAKEPILOT_HOST` | no | `127.0.0.1` | Bind address (an IP address) |
| `FLAKEPILOT_PORT` | no | `8000` | Bind port |
| `FLAKEPILOT_PUBLIC_ORIGIN` | for sign-in | — | The public `https://host[:port]` users reach. Used for the OAuth callback and the CSRF origin check |
| `FLAKEPILOT_GITHUB_CLIENT_ID` | for sign-in | — | GitHub App client ID |
| `FLAKEPILOT_GITHUB_CLIENT_SECRET` | for sign-in | — | GitHub App client secret. Never logged |
| `FLAKEPILOT_ALLOW_INSECURE_COOKIES` | no | `false` | `true` only for local plain-HTTP development: drops `Secure` and the `__Host-` cookie prefix. Never in production |
| `FLAKEPILOT_SECRET_KEY` | no | — | 32 random bytes, base64, for the encrypted secret store. Validated if set, but **not used yet** |
| `FLAKEPILOT_ARTIFACT_DIR` | for ingestion | — | Where raw test reports are stored. Must be an existing absolute directory, owned by the service user, not group- or world-writable, and outside the repository. Unset: `POST /ingest/junit` answers 503 `ingestion_not_configured` |
| `FLAKEPILOT_WORKER_POLL_SECONDS` | no | `2` | Worker poll interval, 1–300. The API process ignores it |
| `FLAKEPILOT_WORKER_BATCH_SIZE` | no | `10` | Jobs and outbox rows per tick, 1–100 |
| `FLAKEPILOT_WORKER_LEASE_SECONDS` | no | `30` | Job lease length, 5–3600 |
| `FLAKEPILOT_EXECUTION_MODE` | no | `closed` | `closed` never starts a container. `evaluation` is an explicit opt-in and is not production isolation |
| `FLAKEPILOT_EXECUTION_RUNTIME` | no | `docker` | `docker` or `podman`, used only in evaluation mode |
| `FLAKEPILOT_EXECUTION_IMAGE` | for evaluation | — | Digest-pinned image. Required when mode is `evaluation` |
| `FLAKEPILOT_EXECUTION_WALL_SECONDS` | no | `30` | Sandbox wall clock, 5–120. The execution lease is this plus 30 seconds |
| `FLAKEPILOT_EXECUTION_MEMORY_MB` | no | `512` | Container memory limit, 64–2048 |
| `FLAKEPILOT_EXECUTION_PIDS` | no | `64` | Container process limit, 8–256 |
| `FLAKEPILOT_GITHUB_CONTENTS_TOKEN` | no | — | Optional bearer for GitHub contents reads. Empty: revisions stay unverified. Never logged |
| `FLAKEPILOT_AI_ENDPOINT` | no | — | Operator-run local inference server URL (http(s), ≤500 chars) for advisory assist (ADR-017). Empty: no model contact at all. Never logged |
| `FLAKEPILOT_AI_MODEL` | with `..._AI_ENDPOINT` | — | Model identifier (≤80 chars) recorded on every response. Required when the endpoint is set; refused (configures nothing) without it |
| `FLAKEPILOT_AI_API_KEY` | no | — | Optional API key for the endpoint. Never logged. Unset also fine for keyless local servers |
| `FLAKEPILOT_GITHUB_APP_ID` / `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` | for session views | — | GitHub App id and unencrypted RSA key, together, for the code-host access verifier. Unset: session views and the investigation answer 404 (fail closed). Installation-token minting was live-checked on 2026-10-25: contents:read through one commit confirmation, and actions:read through `backfill-ci-history`'s run and artifact listings. The session-view verifier was not run live |
| `FLAKEPILOT_SENTRY_DSN` | no | — | `https://<public key>@<host>/<project id>`. API process only. Unset: nothing is reported. Set: ERROR records become allow-listed events ([SENTRY-OBSERVABILITY-BOUNDARY](docs/operations/SENTRY-OBSERVABILITY-BOUNDARY.md)) |
| `FLAKEPILOT_AUTH0_DOMAIN` / `FLAKEPILOT_AUTH0_AUDIENCE` | no | — | Auth0 tenant host and API identifier, together. Set: `POST /agent/orgs/{org_id}/runs/{run_id}/investigation` exists for agents an org admin bound with `bind-agent` ([AUTH0-AGENT-ROUTE](docs/operations/AUTH0-AGENT-ROUTE.md)). Unset: that route does not exist |
| `FLAKEPILOT_GEMINI_API_KEY` / `FLAKEPILOT_GEMINI_MODEL` | no | — | Hosted Gemma for assist, together; model `gemma-4-31b-it` or `gemma-4-26b-a4b-it`. Refused beside `FLAKEPILOT_AI_ENDPOINT` |
| `FLAKEPILOT_SERPAPI_API_KEY` | no | — | SerpApi research stage. Sends the run's failing test names as the search query |
| `FLAKEPILOT_BROWSERBASE_API_KEY` / `FLAKEPILOT_BROWSER_ALLOWLIST` | no | — | Browserbase browse stage, together; comma-separated exact public host names |
| `FLAKEPILOT_ELEVENLABS_API_KEY` / `FLAKEPILOT_ELEVENLABS_VOICE_ID` | no | — | ElevenLabs spoken brief, together |
| `FLAKEPILOT_ELEVENLABS_MODEL` | no | `eleven_flash_v2_5` | Or `eleven_multilingual_v2`; refused without the key |
| `FLAKEPILOT_TABPFN_API_KEY` | no | — | TabPFN candidate for `evaluate-prioritization --with-tabpfn` only |
| `FLAKEPILOT_ATLAS_URI` / `FLAKEPILOT_ATLAS_DATABASE` | no | — | MongoDB Atlas, together, read only by `export-reports-atlas` (a write-only copy; PostgreSQL stays the system of record and Atlas is never read back). The URI must keep TLS on; `tls=false` and `tlsInsecure`-style options are refused. One without the other refuses startup |
| `FLAKEPILOT_TEMPORAL_ADDRESS` / `FLAKEPILOT_TEMPORAL_NAMESPACE` | no | — | Temporal `host:port` and namespace, together, read only by the `temporal-*` proof-of-concept commands. The API process and the default worker never connect |
| `FLAKEPILOT_TEMPORAL_API_KEY` | no | — | Temporal Cloud API key; turns TLS on. Refused without the address |
| `FLAKEPILOT_INGEST_RATE_LIMIT` / `FLAKEPILOT_INGEST_RATE_WINDOW_SECONDS` | no | 120 / 3600 | Ingest cost control: the processed uploads one CI token may cause in the pair over the trailing window, in seconds. Over the limit: 429 `rate_limited` (the refusal is a recorded, audited rejection). Duplicates count, so a retry loop is not a free pass |
| `FLAKEPILOT_AI_DAILY_REQUEST_CAP` | no | 500 | AI spend control: the provider-bound assist requests one org may make per UTC day when its policy is enabled. Reached: the model is not called (`ai_daily_cap`); the evidence path and policy are unchanged. ADR-014's "no quota store" concern does not apply: the count is the org's own `diag.ai_requests` rows |

### GitHub App sign-in

1. Create a GitHub App.
2. Set its callback URL to `{FLAKEPILOT_PUBLIC_ORIGIN}/auth/github/callback`, for example `https://flakepilot.example.com/auth/github/callback`.
3. Set `FLAKEPILOT_PUBLIC_ORIGIN`, `FLAKEPILOT_GITHUB_CLIENT_ID` and `FLAKEPILOT_GITHUB_CLIENT_SECRET`.

Without a client ID and secret, sign-in is off and the auth endpoints answer 503 `auth_not_configured`; a public origin on its own changes nothing. If the client ID or secret is set but any of the three is missing, **startup fails** (fail closed). Sign-in only authenticates; access comes from FlakePilot's own org membership and grants (ADR-011).

### Advisory AI endpoint (ADR-017)

Optional, operator-run, local. Set `FLAKEPILOT_AI_ENDPOINT` and `FLAKEPILOT_AI_MODEL` (plus `FLAKEPILOT_AI_API_KEY` if the server expects one) to point assist at your own inference server; unset keeps the shipped `NullAiAssist` posture — no model contact, assist answers `unavailable`, every other capability unchanged. The endpoint is contacted only when an org admin has enabled assist policy, and model output is advisory data: it never changes the deterministic `ti-classification/1` verdict or grants anything. Model identity is recorded on every response; keys and endpoint are never logged.

### Hosted providers (ADR-017)

Every hosted provider is off, with no network contact, unless its settings are complete; a pair with one half set, and a value the adapter refuses, stop startup without echoing a key. They are read by the API process and the operator commands only — never give them to the worker. At startup the API logs one line with each provider's code (`hosted providers configured (not checked at runtime): ai_assist=… research=…`); `provider-status` prints the same as JSON. "Configured" is not "working": the process never checks a provider's health. Which adapters have been run against the live service, and how, is recorded in [SPONSOR-IMPLEMENTATION-MAP](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md#live-verification-record-2026-10-24).

| Provider | Variables | Used by | What leaves the process |
|---|---|---|---|
| Gemma via the Gemini API | `FLAKEPILOT_GEMINI_API_KEY`, `FLAKEPILOT_GEMINI_MODEL` | hypothesize stage, after the org enables AI policy | the stage instructions and the run's evidence parts (test classname, name, id, attempt outcomes). Owner decision first: Gemma on the Gemini API is free-tier only, and Google may use free-tier prompts |
| SerpApi | `FLAKEPILOT_SERPAPI_API_KEY` | research stage | the failing tests' names as the search query |
| Browserbase Fetch | `FLAKEPILOT_BROWSERBASE_API_KEY`, `FLAKEPILOT_BROWSER_ALLOWLIST` | browse stage | research-source URLs on the allowlist; pages come back as hashed evidence, their text never reaches the model |
| ElevenLabs | `FLAKEPILOT_ELEVENLABS_API_KEY`, `FLAKEPILOT_ELEVENLABS_VOICE_ID` (`FLAKEPILOT_ELEVENLABS_MODEL`) | `POST …/investigation/brief` only | the counts-only brief text (no test names, logs or model text) |
| TabPFN (Prior Labs) | `FLAKEPILOT_TABPFN_API_KEY` | `evaluate-prioritization --with-tabpfn` only | numeric feature rows and 0/1 labels |
| Sentry | `FLAKEPILOT_SENTRY_DSN` | API error reporting | allow-listed event fields only |
| MongoDB Atlas | `FLAKEPILOT_ATLAS_URI`, `FLAKEPILOT_ATLAS_DATABASE` | `export-reports-atlas` only | classified run reports: IDs, counts, enum values, commit SHA, environment, attempt outcomes and verdicts with evidence; never test names, upload metadata or artifact keys |
| Temporal | `FLAKEPILOT_TEMPORAL_ADDRESS`, `FLAKEPILOT_TEMPORAL_NAMESPACE` (`FLAKEPILOT_TEMPORAL_API_KEY`) | `temporal-*` commands only | workflow input and result: the investigation key, org, run and member GitHub IDs, the stopping condition and a stage count |

### Investigation (session users)

`POST /orgs/{org_id}/runs/{run_id}/investigation` runs the bounded investigation of one stored run: collect, research, browse, hypothesize, prioritize, then one `diag.investigation.recorded` provenance row. It answers JSON with each stage's status and reason (an unconfigured provider is a `skipped` stage with reason `provider_absent`, never an error), the research sources (URL, title, time), captured page hashes, the assist's counts (hypothesis text stays in the diagnosis store), the history ranking, the counts-only brief text, and `report_url` (the run view). `POST /orgs/{org_id}/runs/{run_id}/investigation/brief` runs it again and answers the brief as audio (`Cache-Control: no-store`; the audio is not stored) with the run view in a `Link: <url>; rel="related"` header — the URL is never spoken — only when ElevenLabs is configured, else 503 `speech_not_configured`.

Both are POSTs with the session cookie, so the CSRF same-origin rule applies. The chain is the run view's, with `repo.investigate` in place of `repo.view`: a run outside the caller's scope, an unverified code-host standing, and a member without any grant on the repo are the same 404 `not_found`; a member who can see the repo but lacks `repo.investigate` gets 403 `forbidden`. Refusals are recorded as provenance too. Nothing here executes code or changes a verdict.

### Session-user run views

`GET /orgs/{org_id}/repos/{repo_id}/runs` lists one repository's stored runs, newest first (default page 50, hard bound 200), and `GET /orgs/{org_id}/runs/{run_id}` is the one read-only view of a stored run — summary counts, upload metadata, attempt chains, and each test's `ti-classification/1` verdict with its evidence (same rendering rules as the operator report: everything user-controlled is HTML-escaped, `inconclusive` is shown as the real verdict, revision status stays `unchecked`). The listing carries exactly the stored run rows — source, commit SHA, environment, revision status, received time, summary counts — and links each run to its run view; per-test verdicts live on each run's own view. Both pages state plainly that nothing is verified or executed here.

Authorization chain (identical for both routes): the session cookie authenticates the user; the org and repo in the URL are only lookup keys — access comes from IAM's membership and `repo_grants` records for that user (`repo.view` is required, decided exactly as every other session read, after the code-host standing REPO's `CodeHostAccessVerifier` verified for the exact (org, repo) pair, fail closed). An unbound repository, an unavailable or ambiguous verification, a non-member, a member without `repo.view`, and a resource of another org are all answered with the same 404 `not_found`: a cross-org request learns nothing. No session (bad or missing cookie) is 401 `unauthenticated`. A signed-in member whose repo has stored runs but empty evidence sees an empty listing, not an error. There is no other UI and no link fabricating one.

## Run the API

```sh
uv run alembic upgrade head        # migrations are an explicit step, never run at startup
uv run python -m flakepilot.interface.composition_root
```

If PostgreSQL is unreachable at startup, the process exits with code 3, so use a restart policy or wait on a database health check. Probes: `GET /health/live` and `GET /health/ready` (the latter includes a bounded database check).

Database timeouts are constants in `src/flakepilot/interface/composition_root/database.py`. Local-development defaults:
- 30 s per statement;
- 60 s idle in a transaction;
- 5 s to connect or to get a pooled connection;
- 2 s for readiness.

Pooled connections are pinged on checkout.

Sign-in endpoints: `GET /auth/github/login`, `GET /auth/github/callback` and `POST /auth/logout` (ADR-011). Signing in grants nothing by itself. An operator creates the first org and its org admin; the command prints only the new org ID. Org names are unique ignoring case:

```sh
uv run python -m flakepilot.interface.composition_root bootstrap-org --name "Acme" --admin-github-id 12345 --admin-login octocat
```

Grant administration (register repositories, add members, grant or revoke
permissions) is also operator CLI only — no HTTP route (ADR-015). Session-admin
granting is blocked until a code-host access verifier exists (ADR-011 I), so
these commands carry the same operator trust as `bootstrap-org`; every
mutation is audited with the operator as actor:

```sh
uv run python -m flakepilot.interface.composition_root register-repo --org-id $ORG_ID --repo-id $REPO_ID --owner acme --name payments
uv run python -m flakepilot.interface.composition_root add-member --org-id $ORG_ID --member-github-id 67890 --member-login demo-member
uv run python -m flakepilot.interface.composition_root grant --org-id $ORG_ID --member-github-id 67890 --repo-id $REPO_ID --grant repo.view
uv run python -m flakepilot.interface.composition_root revoke --org-id $ORG_ID --member-github-id 67890 --repo-id $REPO_ID --grant repo.view
uv run python -m flakepilot.interface.composition_root show-access --org-id $ORG_ID --member-github-id 67890 [--repo-id $REPO_ID]
uv run python -m flakepilot.interface.composition_root set-ai-policy --org-id $ORG_ID --admin-github-id 12345 --level evidence_only [--retain-raw]
```

`set-ai-policy` acts for the named org admin (anyone else is refused) through
the same `Assist.set_policy` use case as the product, audited as
`diag.ai_policy.updated`; `--level disabled` turns assist off again.

`bind-agent` and `unbind-agent` let an org admin delegate one bounded
investigation scope to an Auth0 machine-to-machine client (goal 9; needs
`FLAKEPILOT_AUTH0_*` for the route to exist). Only the token subject's SHA-256
is stored, at most 8 hours, scopes within `repo.view`/`repo.investigate`,
audited as `iam.agent.bound`/`iam.agent.unbound`. Details and every answer
code: [AUTH0-AGENT-ROUTE](docs/operations/AUTH0-AGENT-ROUTE.md).

```sh
uv run python -m flakepilot.interface.composition_root bind-agent --org-id $ORG_ID \
  --admin-github-id 12345 --repo-id $REPO_ID --auth0-subject '<client_id>@clients' \
  --agent-name mastra_triage --scope repo.investigate --ttl-minutes 60
uv run python -m flakepilot.interface.composition_root unbind-agent --org-id $ORG_ID \
  --admin-github-id 12345 --auth0-subject '<client_id>@clients'
```

Repositories must be registered in the org before a member can be granted
access; `show-access` renders the standing per repo through the same domain
rules `decide()` uses. Membership and grant changes revoke the member's live
sign-in sessions. A foreign-org or unregistered repo is refused with one
message (the org is not named), and every refusal exits 1.

`list-runs` lists one repository's stored runs, newest first (default page
50, hard bound 200), read-only; it prints exactly the run IDs
`ci-run-report` and the session run views accept, so the stored runs are
discoverable without CI-side bookkeeping:

```sh
uv run python -m flakepilot.interface.composition_root list-runs \
  --org-id $ORG_ID --repo-id $REPO_ID            # or --format json
```

Read-only analytics, JSON on stdout (operator trust, like `list-runs`):

```sh
uv run python -m flakepilot.interface.composition_root provider-status
uv run python -m flakepilot.interface.composition_root reliability-trend \
  --org-id $ORG_ID --repo-id $REPO_ID --days 7 --bucket day
uv run python -m flakepilot.interface.composition_root evaluate-prioritization \
  --org-id $ORG_ID --repo-id $REPO_ID --train-cutoff 2026-10-01T00:00:00+00:00 \
  --horizon-days 7 [--k 10] [--with-tabpfn]
```

`backlog-status` counts durable work that is not done: per outbox (`iam`, `ti`,
`diag`, `exec`, `repo`) the undelivered rows (`published_at` null, not dead),
the dead rows (`dead_at` set) and the oldest undelivered row's age in seconds,
plus `orch.jobs` by state with the oldest scheduled job's age. Counts and ages
only — never event names, correlation IDs or payloads — so the output is safe
to paste into a ticket. Exit code is 0 even with backlogged rows: the number
is the message; alerting decides.

```sh
uv run python -m flakepilot.interface.composition_root backlog-status
```

`reliability-trend` compares failure and flaky rates per bucket and environment with the previous window (exact arithmetic, `incomplete` when a read hits its bound). `evaluate-prioritization` scores a candidate against the shipped prioritization baseline on a temporal split that never lets a feature see the label window; without `--with-tabpfn` (and its key) the candidate is reported `blocked`, never invented. Both read a run's CI time: the original completion time for a backfilled run, receipt time otherwise.

`backfill-ci-history` (operator trust, audited) imports the JUnit XML that a registered repository's GitHub Actions runs uploaded as artifacts. Each run keeps its original completion time (`ti.runs.ci_completed_at`) and explicit provenance (`ci_source` `github_actions_backfill`, the original run ID, attempt and URL), and `received_at` stays the import time. It needs `FLAKEPILOT_GITHUB_APP_ID`, `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` (the App must have actions:read) and `FLAKEPILOT_ARTIFACT_DIR`. The repository is the pair's registered binding, and the token is actions:read on that one repository. A re-import of the same (run, attempt, artifact) is a no-op. Bounds and archive safety are in [TABPFN-PRIORITIZATION-EVALUATION](docs/operations/TABPFN-PRIORITIZATION-EVALUATION.md#real-history-the-ci-backfill):

```sh
uv run python -m flakepilot.interface.composition_root backfill-ci-history \
  --org-id $ORG_ID --repo-id $REPO_ID [--max-runs 50] [--workflow ci.yml] \
  [--branch main] [--artifact-pattern 'junit-*']
```

FlakePilot's own CI uploads its pytest JUnit report as the `junit-gates`
artifact on every run (failing runs included), so `--artifact-pattern
'junit-*'` imports this repository's own history.

`export-reports-atlas` (operator trust, opt-in) copies a pair's newest
classified run reports (`--limit`, 1-200) to MongoDB Atlas with an
upsert-only key (org, repo, run, SHA-256 of the document), so a re-export
writes nothing. Each outcome is a `ti.report.exported` or
`ti.report.export_failed` event with a reason code; the first failure stops
the batch ([MONGODB-ATLAS-EVALUATION](docs/operations/MONGODB-ATLAS-EVALUATION.md)):

```sh
uv run python -m flakepilot.interface.composition_root export-reports-atlas \
  --org-id $ORG_ID --repo-id $REPO_ID [--limit 50]
```

The Temporal proof of concept (opt-in, never the default worker) runs one
investigation per workflow while FlakePilot's advisory lock and existing
`diag.investigation.recorded` row stay authoritative
([TEMPORAL-DURABLE-WORKFLOW-EVALUATION](docs/operations/TEMPORAL-DURABLE-WORKFLOW-EVALUATION.md)):

```sh
uv run python -m flakepilot.interface.composition_root temporal-worker
uv run python -m flakepilot.interface.composition_root temporal-run-investigation \
  --org-id $ORG_ID --run-id $RUN_ID --member-github-id 12345 [--key $UUID]
uv run python -m flakepilot.interface.composition_root temporal-cancel-investigation --key $UUID
```

`verify-github-app` is a read-only staging diagnostic for the GitHub App and
the code-host access verifier (the Decision E runbook in
[GRANT-AUTHORIZATION-DECISION-SHEET](docs/operations/GRANT-AUTHORIZATION-DECISION-SHEET.md)).
It verifies explicitly named collaborator handles against an explicitly
registered (org, repo) binding through the real composition verifier, always
runs a no-credentials fail-closed probe, counts every provider contact, and
exits nonzero unless the requested live checks pass against a contacted
provider. With no credentials configured it reports LIVE VERIFICATION NOT
PERFORMED; it mutates nothing (no grant, membership, session, audit or outbox
row):

```sh
uv run python -m flakepilot.interface.composition_root verify-github-app \
  --label staging --org-id $ORG_ID --repo-id $REPO_ID --owner acme --name staging-pilot \
  --allow-login collaborator-ok --deny-login not-a-collaborator
```

The authenticated session-user run views (the runs listing and the run view)
verify code-host access before serving: REPO's `CodeHostAccessVerifier`
implementation checks the viewer's account handle against the exact canonical
(org, repo) pair as FlakePilot's GitHub App installation (ADR-016), and
`repo.view` is then decided under PRODUCT-SCOPE §3.1's intersection rule. A
deployment without `FLAKEPILOT_GITHUB_APP_ID` and
`FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` configured cannot verify anything, so the
authenticated routes answer 404 (fail closed, without disclosure) until the
App is configured; the operator CLI report is unaffected. Verification says
nothing about which commit produced a run: the revision status stays
`unchecked`.

The worker is a second process. It does not serve HTTP. Stop it with `SIGINT` or `SIGTERM`; it finishes the batch it has already claimed.

```sh
uv run python -m flakepilot.interface.composition_root worker
```

CI uploads JUnit XML with a CI upload token (issued by the operator CLI `issue-ci-upload-token`, see the demo journey above):

- **Request:** `POST /ingest/junit` with `Authorization: Bearer fpi_…`, `Content-Type: application/xml`, and the report as the body (10 MiB at most).
- **Metadata headers**, each sent exactly once: `X-FlakePilot-CI-Source`, `-CI-Run-Id`, `-CI-Attempt`, `-Job-Name`, `-Commit-Sha`, `-Ref`, `-Environment`, `-Started-At` and `-Finished-At`.
- **Scope:** the token decides the org and repo.
- **Answers:** 201 accepted; 200 a duplicate of an earlier upload with the same idempotency key and content; 409 `idempotency_conflict` for the same key with other content.
- **Details:** formats, limits and deviations are in ADR-013.

Errors are JSON `{"error": {"code", "message", "request_id"}}`; the request ID is also in the `X-Request-ID` header. Stable codes:

| Code | Meaning |
|---|---|
| `unauthenticated` | No verified principal |
| `forbidden` | Visible resource, action not permitted |
| `not_found` | No such route, or a resource the principal can't see |
| `method_not_allowed` | Wrong HTTP method |
| `validation_error` | Request failed validation |
| `http_error` | Any other HTTP error status |
| `internal_error` | Unexpected server error |
| `auth_not_configured` | Sign-in is not configured |
| `auth_provider_unavailable` | The sign-in provider is down |
| `idempotency_conflict` | Same idempotency key, other content |
| `payload_too_large` | Body over the limit |
| `unsupported_media_type` | Wrong content type or encoding |
| `invalid_report` | Report refused, with a `reason` |
| `ingestion_not_configured` | No artifact directory is configured |
| `speech_not_configured` | No speech provider is configured for the spoken brief |
| `speech_unavailable` | The speech provider refused or was unreachable, with a `reason` |

API conventions: `src/flakepilot/interface/http/__init__.py`.

Each context keeps its migrations in its own `infrastructure/migrations` directory and its tables in its own schema (`iam`, `int_secrets`, `ti`, `diag`, `orch`, `aud`, `repo`, `exec`). `uv run alembic check` verifies that the models and migrations match.

## Quality gates

The same commands run in CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Toolchain and policy: [ADR-010](docs/adr/ADR-010-engineering-toolchain-and-quality-gates.md).

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
```

- `pip-licenses` checks installed Python packages only. Shipped container images need the separate licence review in [ADR-007](docs/adr/ADR-007-implementation-stack-and-licensing.md) item 5.
- The dev-environment licence gate has one owner-approved exception (pathspec 1.1.1, MPL-2.0, dev-only via mypy), recorded in `pyproject.toml` next to `[tool.pip-licenses]`. The runtime-environment gate allows no exceptions.
- Both gates read one `allow-only` list. On 2026-10-25 the owner added `ISC` (dnspython, required by pymongo) and `3-Clause BSD License` (protobuf's spelling of BSD-3-Clause, required by temporalio) to it. That is a change to the list, not an exception.
- Tests marked `integration` need PostgreSQL and are skipped unless `FLAKEPILOT_TEST_DATABASE_URL` is set. When `CI` is set (GitHub Actions), a missing URL fails the run instead; CI provides a PostgreSQL service.
- Integration tests create and drop a throwaway database for each test or module next to `FLAKEPILOT_TEST_DATABASE_URL`, so that role needs `CREATEDB`. **Never point it at a real database.**

## Security

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Licence

[Apache-2.0](LICENSE).
