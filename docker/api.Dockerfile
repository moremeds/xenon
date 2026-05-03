# syntax=docker/dockerfile:1.7
# FastAPI + IB pool + UW services. Reaches IB Gateway via host.docker.internal
# at runtime; quiet at boot when the gateway isn't reachable.

FROM python:3.13-slim AS builder

# uv resolves the interpreter from .python-version + pyproject.toml and
# keeps uv.lock authoritative. Pin uv via the official static binary image
# rather than pip-installing it inside the slim base.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

# Build deps for any wheels without prebuilt aarch64 binaries (asyncpg,
# cryptography, etc. ship wheels — keep this list minimal but present so
# the builder can fall back to source compile without surprise failures).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only manifests first so the dep-install layer caches across code
# changes. README.md is read by hatchling during the editable install of the
# `xenon` package itself (pyproject.toml declares `readme = "README.md"`).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --no-dev skips test/dev extras; --frozen ensures uv.lock is the source of truth.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev

# ---- runtime stage ----
FROM python:3.13-slim AS runtime

# libpq for psycopg's binary wheel runtime path; libssl for asyncpg/cryptography.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring over the resolved venv and the source tree. Migrations stay in src
# under the same path the api expects via xenon.db.migrations imports.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml uv.lock alembic.ini ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XENON_API_HOST=0.0.0.0 \
    XENON_API_PORT=8321

EXPOSE 8321

# tini is PID 1 so SIGTERM from `docker stop` cleanly shuts uvicorn down.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "xenon.api.server:app", "--host", "0.0.0.0", "--port", "8321"]

LABEL org.opencontainers.image.source="https://github.com/moremeds/xenon" \
      org.opencontainers.image.title="xenon-api" \
      org.opencontainers.image.description="Xenon FastAPI bridge"
