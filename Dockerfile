# FlakePilot control plane image: the API and the worker run this one image with
# different commands (render.yaml). No secrets are baked in; every setting comes
# from the environment at runtime. The image never executes repository code:
# execution stays closed (ClosedSandbox) and no container runtime is installed.
# Base images are digest-pinned; bump them deliberately, never implicitly.

FROM ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 AS uv

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin flakepilot
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Alembic reads [tool.alembic] from ./pyproject.toml; migrations ship in the package.
COPY pyproject.toml ./
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLAKEPILOT_HOST=0.0.0.0 \
    FLAKEPILOT_PORT=10000
USER flakepilot
EXPOSE 10000
CMD ["python", "-m", "flakepilot.interface.composition_root"]
