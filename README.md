# FlakePilot

Evidence-based investigation and verification of recurring test failures. See [PRODUCT-SCOPE](docs/product/PRODUCT-SCOPE.md).

## Status

**Pre-alpha. Not production-ready.** The backend foundations exist: sign-in, org bootstrap, CI upload tokens, JUnit ingestion and a failure classifier. Known gaps:

- **No UI beyond one read-only page.** The session-user run view (`GET /orgs/{org_id}/runs/{run_id}`) is the only server-rendered HTML. It is read-only, requires a signed-in member with `repo.view`, and is answered 503 `auth_not_configured` when sign-in is not configured, like the other session routes.
- **CI upload tokens are issued only by the operator CLI** (`issue-ci-upload-token`, see the demo journey below). No HTTP route exists for them yet.
- **Classification isn't exposed over HTTP.** It exists as an authorization-checked use case (ADR-013), and the operator CLI's `ci-run-report` attaches each test's verdict — deterministic rule set `ti-classification/1`, with the evidence it was computed from — neither of which is an HTTP route.
- **Parsing is synchronous.** Reports are parsed inside the upload request. A separate worker process runs durable jobs and delivers outbox rows to the audit log. No retention or stage job is registered. See [DURABLE-JOBS](docs/operations/DURABLE-JOBS.md).
- **No quotas or rate limits.** AI spend is not checked either (ADR-014).
- **AI assist is off unless an org admin enables it.** The use case exists, the process wires only `NullAiAssist`, and there is no HTTP route. A missing provider answers `unavailable`. See ADR-014.
- **Artifacts are not encrypted at rest, and never cleaned up.** Raw reports are plain files under `FLAKEPILOT_ARTIFACT_DIR`, and there is no retention job.
- **Browser reproduction fails closed.** The default execution mode starts no container and runs no repository code. Evaluation mode is an explicit opt-in, builds a `docker` or `podman` argument list, and is not a microVM and not a production sandbox. No container was run in CI. See [EXECUTION](docs/operations/EXECUTION.md).
- **No Docker deployment yet.**
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

One command runs the whole journey (`createdb flakepilot_demo` once beforehand):

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

6. **See the stored run** (new in this MVP; read-only operator report):

   ```sh
   uv run python -m flakepilot.interface.composition_root ci-run-report \
       --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" \
       --format html --out demo-run.html && open demo-run.html
   ```

   The report shows the run's summary counts, the upload metadata, each test's
   attempt chain, and each test's **classification** — the deterministic,
   versioned rule set `ti-classification/1` applied to the stored CI evidence
   (no confidence scores, no model output). On the demo data: the flaky test
   is classified `flaky` at the failing commit with the passing and failing
   attempt counts as evidence; the failing test is classified
   `deterministic_regression` **because a real earlier stored run passed it in
   the same environment** (`baseline_sha` names that run's commit); a test
   that only passed reports a truthful `inconclusive` /
   `insufficient_history`. The commit SHA is what CI reported, the revision
   status stays `unchecked`, and the raw report body is stored as a plaintext
   file. `--format json` is available for scripts.

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

### GitHub App sign-in

1. Create a GitHub App.
2. Set its callback URL to `{FLAKEPILOT_PUBLIC_ORIGIN}/auth/github/callback`, for example `https://flakepilot.example.com/auth/github/callback`.
3. Set `FLAKEPILOT_PUBLIC_ORIGIN`, `FLAKEPILOT_GITHUB_CLIENT_ID` and `FLAKEPILOT_GITHUB_CLIENT_SECRET`.

Without a client ID and secret, sign-in is off and the auth endpoints answer 503 `auth_not_configured`; a public origin on its own changes nothing. If the client ID or secret is set but any of the three is missing, **startup fails** (fail closed). Sign-in only authenticates; access comes from FlakePilot's own org membership and grants (ADR-011).

### Session-user run view

`GET /orgs/{org_id}/runs/{run_id}` is the one server-rendered page for signed-in users: a read-only view of one stored run — summary counts, upload metadata, attempt chains, and each test's `ti-classification/1` verdict with its evidence (same rendering rules as the operator report: everything user-controlled is HTML-escaped, `inconclusive` is shown as the real verdict, revision status stays `unchecked`). It states plainly that nothing is verified or executed here.

Authorization chain: the session cookie authenticates the user; the org in the URL is only a lookup key — access comes from IAM's membership and `repo_grants` records for that user (`repo.view` is required, decided exactly as every other session read). A non-member, a member without `repo.view`, and a run of another org are all answered with the same 404 `not_found`: a cross-org request learns nothing. No session (bad or missing cookie) is 401 `unauthenticated`. There is no UI beyond this page and no link fabricating one.

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

API conventions: `src/flakepilot/interface/http/__init__.py`.

Each context keeps its migrations in its own `infrastructure/migrations` directory and its tables in its own schema (`iam`, `int_secrets`, `ti`, `diag`, `orch`, `aud`, `repo`, `exec`). `uv run alembic check` verifies that the models and migrations match.

## Quality gates

The same commands run in CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Toolchain and policy: [ADR-010](docs/adr/ADR-010-engineering-toolchain-and-quality-gates.md).

```sh
uv lock --check
uv sync --locked
uv run pytest
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
- Tests marked `integration` need PostgreSQL and are skipped unless `FLAKEPILOT_TEST_DATABASE_URL` is set. When `CI` is set (GitHub Actions), a missing URL fails the run instead; CI provides a PostgreSQL service.
- Integration tests create and drop a throwaway database for each test or module next to `FLAKEPILOT_TEST_DATABASE_URL`, so that role needs `CREATEDB`. **Never point it at a real database.**

## Security

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Licence

[Apache-2.0](LICENSE).
