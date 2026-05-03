# syntax=docker/dockerfile:1.7
# One-shot alembic migrator. Runs `alembic upgrade head` against DATABASE_URL
# and exits. Gated behind `profiles: ["migrate"]` in docker-compose.yml so
# `docker compose up -d` never auto-runs schema changes — surprise migrations
# at market open are explicitly out of scope.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Same dep tree as the api image (alembic env.py imports xenon.db.schema, so
# we need the full xenon package). Trying to slim further by carving out only
# alembic+sqlalchemy+asyncpg would diverge the migrator's resolved versions
# from what the api ships — schema mismatches at deploy time are a worse
# failure mode than a slightly larger one-shot image.
# README.md is required by hatchling for the editable install (pyproject
# declares `readme = "README.md"`).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev

# ---- runtime stage ----
FROM python:3.13-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml uv.lock alembic.ini ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["alembic", "upgrade", "head"]

LABEL org.opencontainers.image.source="https://github.com/moremeds/xenon" \
      org.opencontainers.image.title="xenon-migrator" \
      org.opencontainers.image.description="Xenon alembic one-shot migrator"
