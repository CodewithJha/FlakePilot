# FlakePilot

Evidence-based investigation and verification of recurring test failures. See [PRODUCT-SCOPE](docs/product/PRODUCT-SCOPE.md).

## Status

**Pre-alpha. Not production-ready.** The backend foundations exist: sign-in, org bootstrap, CI upload tokens, JUnit ingestion and a failure classifier. Known gaps:

- **No UI.** The API is the only interface.
- **CI upload tokens can't be issued yet.** The use case exists, but there is no HTTP route or CLI for it.
- **Classification isn't exposed over HTTP.** It exists only as an application use case (ADR-013).
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

CI uploads JUnit XML with a CI upload token (no issuance route yet, see Status):

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
