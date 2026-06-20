# syntax=docker/dockerfile:1

# --- Frontend build (Vite SPA) ---
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* frontend/.npmrc ./
RUN npm ci
COPY frontend/ .
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=$APP_VERSION
RUN npm run build

# --- Backend runtime (uv-managed, Python 3.13) ---
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -r -s /bin/false appuser
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock* ./
# No BuildKit cache mount: Saturn's Docker 24 defaults to the classic builder, and
# the passwordless-sudo exclusion only covers `docker` (not `env`/buildkit). Plain
# RUN keeps one Dockerfile that builds on both classic and BuildKit (CI).
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
COPY --from=frontend /build/dist /app/frontend/dist
COPY docker-entrypoint.sh /docker-entrypoint.sh
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
ENV PATH="/app/.venv/bin:$PATH"
# The project isn't pip-installed (uv sync --no-install-project), and console scripts
# (alembic, uvicorn) don't put CWD on sys.path — so make /app importable explicitly.
ENV PYTHONPATH=/app
RUN chmod +x /docker-entrypoint.sh \
    && mkdir -p /app/data/originals /app/data/assets/thumbs /app/data/assets/mobile /app/data/assets/web \
    && chown -R appuser:appuser /app/data

USER appuser
ENTRYPOINT ["/docker-entrypoint.sh"]
