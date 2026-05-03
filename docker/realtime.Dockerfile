# syntax=docker/dockerfile:1.7
# IB realtime WebSocket relay (Node). Connects to IB Gateway via
# host.docker.internal at runtime; resolves port from XENON_TRADING_MODE.

FROM node:22-alpine AS builder

RUN apk add --no-cache libc6-compat

WORKDIR /app

# The relay imports from ../../lib (lru-cache, rate-limiter) at the repo
# root and reads env from ../../../.env at runtime. Carry the root
# package.json (which defines ws/dotenv/ib for the relay), the relay source,
# and the lib helpers it imports.
COPY package.json package-lock.json ./
RUN npm install --no-audit --no-fund --legacy-peer-deps --omit=dev

# ---- runtime stage ----
FROM node:22-alpine AS runtime

RUN apk add --no-cache libc6-compat tini

WORKDIR /app

# Carry the resolved node_modules + the relay's source tree. The relay
# resolves dotenv against ../../../.env from its own location, so place the
# script under /app/scripts/infra/ib_realtime/ to preserve that layout —
# /app/.env is the bind-mount target in compose.
COPY --from=builder /app/node_modules ./node_modules
COPY package.json ./
COPY scripts/infra/ib_realtime/ ./scripts/infra/ib_realtime/
# The relay imports `../../lib/lru-cache.js` from
# scripts/infra/ib_realtime/, which resolves to scripts/lib/ at the repo
# root (NOT root-level lib/).
COPY scripts/lib/ ./scripts/lib/

ENV NODE_ENV=production \
    NODE_OPTIONS="--enable-source-maps"

# WS port the relay listens on; xref scripts/infra/ib_realtime/ib_realtime_server.js
EXPOSE 8765

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "scripts/infra/ib_realtime/ib_realtime_server.js"]

LABEL org.opencontainers.image.source="https://github.com/moremeds/xenon" \
      org.opencontainers.image.title="xenon-realtime" \
      org.opencontainers.image.description="Xenon IB realtime WS relay"
