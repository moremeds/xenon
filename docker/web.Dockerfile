# syntax=docker/dockerfile:1.7
# Next.js standalone build. Calls FastAPI inside the compose network via
# XENON_API_BASE_URL, IB realtime relay via NEXT_PUBLIC_REALTIME_WS_URL.

FROM node:22-alpine AS builder

# next/font, sharp etc. need libc6-compat on alpine.
RUN apk add --no-cache libc6-compat

WORKDIR /app

# Copy lockfiles first for dep cache. The web build references the parent
# package-lock.json via outputFileTracingRoot — keep both manifests.
COPY package.json package-lock.json ./
COPY web/package.json web/package-lock.json ./web/

# Install both root + web deps. legacy-peer-deps mirrors release.yml + ci.yml.
RUN npm install --no-audit --no-fund --legacy-peer-deps \
    && cd web && npm install --no-audit --no-fund --legacy-peer-deps

# Now copy the rest of the build inputs. universe.ts is committed and
# linguist-generated=true, so we skip the npm prebuild hook (which calls
# `uv run python ../scripts/infra/dev/generate_universe_ts.py` — uv is not
# available in this image and is not needed when the file is already
# checked in). Calling `next build` directly bypasses the prebuild hook.
COPY web/ ./web/
COPY lib/ ./lib/
COPY brand/ ./brand/
COPY context/ ./context/
# web/lib/structureCatalog.ts imports the canonical options-structures.json
# from docs/trading/. Carry that one subtree (the rest of docs/ is excluded
# via .dockerignore).
COPY docs/trading/ ./docs/trading/

# Build-args for NEXT_PUBLIC_* values that must be inlined into the client
# bundle. CLERK_SECRET_KEY is a runtime-only secret and is intentionally NOT
# accepted as a build-arg.
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=""
ARG NEXT_PUBLIC_IB_REALTIME_WS_URL=""
ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY \
    NEXT_PUBLIC_IB_REALTIME_WS_URL=$NEXT_PUBLIC_IB_REALTIME_WS_URL \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app/web
RUN npx next build

# ---- runtime stage ----
FROM node:22-alpine AS runtime

RUN apk add --no-cache libc6-compat tini

WORKDIR /app

# next.config.mjs sets output: 'standalone' + outputFileTracingRoot to repo
# root. The standalone output therefore lives at /app/web/.next/standalone
# with a top-level web/server.js entrypoint plus a node_modules tree at the
# standalone root.
COPY --from=builder /app/web/.next/standalone ./
COPY --from=builder /app/web/.next/static ./web/.next/static
COPY --from=builder /app/web/public ./web/public

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1

EXPOSE 3000

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "web/server.js"]

LABEL org.opencontainers.image.source="https://github.com/moremeds/xenon" \
      org.opencontainers.image.title="xenon-web" \
      org.opencontainers.image.description="Xenon Next.js terminal"
