# FlakePilot

Evidence-based investigation and verification of recurring test failures. See [PRODUCT-SCOPE](docs/product/PRODUCT-SCOPE.md).

## Local setup

Requires [uv](https://docs.astral.sh/uv/). uv uses its managed CPython 3.12 (`.python-version`), never the host default.

```sh
uv sync --locked
```

## Run the API

```sh
uv run python -m flakepilot.interface.composition_root
```

Settings come from environment variables only (see [`.env.example`](.env.example)); invalid values stop startup with a message naming the variable. `FLAKEPILOT_DATABASE_URL` (`postgresql://USER:PASSWORD@HOST:5432/DBNAME`) is required. If PostgreSQL is unreachable at startup the process exits with code 3, so deployments must use a restart policy or wait on a database health check (to be implemented in the Docker deployment stage). Probes: `GET /health/live`, `GET /health/ready` (includes a bounded database check).

Database timeouts are constants in `src/flakepilot/interface/composition_root/database.py` (local-development defaults: 30s per statement, 60s idle in a transaction, 5s to connect or get a pooled connection, 2s for readiness). Pooled connections are pinged on checkout, so connections dropped by the server are replaced transparently.

Sign-in uses the GitHub App (`GET /auth/github/login`, `GET /auth/github/callback`, `POST /auth/logout`; ADR-011). It is enabled only when `FLAKEPILOT_PUBLIC_ORIGIN`, `FLAKEPILOT_GITHUB_CLIENT_ID` and `FLAKEPILOT_GITHUB_CLIENT_SECRET` are set; otherwise those endpoints answer 503 `auth_not_configured`. Session cookies are `Secure` with the `__Host-` prefix; for local plain-HTTP development only, set `FLAKEPILOT_ALLOW_INSECURE_COOKIES=true` (never in production). `FLAKEPILOT_SECRET_KEY` (32 bytes, base64) is the key for the encrypted secret store.

Signing in grants nothing by itself. An operator creates the first org and its org admin (after migrations); it prints only the new org ID and is not yet audited (audit is deferred):

```sh
uv run python -m flakepilot.interface.composition_root bootstrap-org --name "Acme" --admin-github-id 12345 --admin-login octocat
```

Errors are JSON `{"error": {"code", "message", "request_id"}}` with stable codes (`unauthenticated`, `forbidden`, `not_found`, `method_not_allowed`, `validation_error`, `http_error`, `internal_error`, `auth_not_configured`, `auth_provider_unavailable`); the request id is also in the `X-Request-ID` header. API conventions and deferred decisions (idempotency, pagination): `src/flakepilot/interface/http/__init__.py`.

Migrations are an explicit step, never run at startup. They read `FLAKEPILOT_DATABASE_URL`; each context keeps its revisions in its own `infrastructure/migrations` directory and its tables in its own schema (`iam`, `int_secrets`):

```sh
uv run alembic upgrade head
uv run alembic check
```

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
