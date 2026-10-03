# API, worker and migrations share this image:
#   replay-api                                   (default) HTTP server
#   replay-worker                                job worker
#   alembic -c backend/alembic.ini upgrade head  migrations (run before each deploy)
# Build from the repository root: docker build -f backend/Dockerfile .

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first (cached unless the lockfile changes).
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
COPY packages/stats/pyproject.toml packages/stats/README.md packages/stats/
COPY packages/sdk-python/pyproject.toml packages/sdk-python/README.md packages/sdk-python/
COPY packages/cli/pyproject.toml packages/cli/README.md packages/cli/
RUN uv sync --frozen --no-dev --no-install-workspace --package replay-api

COPY backend backend
COPY packages/stats packages/stats
RUN uv sync --frozen --no-dev --no-editable --package replay-api


FROM python:3.12-slim
ARG RELEASE=dev
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app backend/alembic.ini backend/alembic.ini
COPY --chown=app:app backend/migrations backend/migrations
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    RELEASE=${RELEASE}
USER app
EXPOSE 8000
CMD ["replay-api"]
