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
