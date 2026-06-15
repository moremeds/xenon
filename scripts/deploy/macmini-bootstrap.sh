#!/usr/bin/env bash
# macmini-bootstrap.sh — first-time setup for a fresh Mac mini production host.
#
# Idempotent: every step probes current state and skips if already done. Safe
# to re-run after a partial failure.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/xenon/master/scripts/deploy/macmini-bootstrap.sh | bash
# or, after first clone:
#   ./scripts/deploy/macmini-bootstrap.sh
#
# Environment overrides (defaults shown):
#   XENON_HOME=~/projects/xenon              # repo location
#   XENON_REPO=git@github.com:<owner>/xenon  # clone URL
#   XENON_BRANCH=master                      # branch/tag to check out at bootstrap
#   XENON_TRADING_MODE=paper                 # initial mode for plists; flip to live later via .env
#   XENON_PG_VERSION=17                      # Homebrew postgres version
#   XENON_NODE_VERSION=22                    # Homebrew node version
#   XENON_DB_NAME=xenon_db
#   XENON_DB_ROLE=xenon_app
#
# What this script does NOT do (manual steps remain):
#   - Apple ID sign-in / FileVault enable / SSH key add to GitHub
#   - IB Gateway / TWS / IBC install (run scripts/infra/setup_ibc.sh after this)
#   - Clerk / R2 / UW secret values (you fill these into .env when prompted)
#   - 2FA on first IB Gateway login
#   - Database promotion from laptop (run scripts/deploy/macmini-data-promote.sh
#     from the laptop after this finishes)

set -euo pipefail

# ---------- Config ----------
XENON_HOME="${XENON_HOME:-$HOME/projects/xenon}"
XENON_REPO="${XENON_REPO:-git@github.com:lcxxcllcx/xenon.git}"
XENON_BRANCH="${XENON_BRANCH:-master}"
XENON_TRADING_MODE="${XENON_TRADING_MODE:-paper}"
XENON_PG_VERSION="${XENON_PG_VERSION:-17}"
XENON_NODE_VERSION="${XENON_NODE_VERSION:-22}"
XENON_DB_NAME="${XENON_DB_NAME:-xenon_db}"
XENON_DB_ROLE="${XENON_DB_ROLE:-xenon_app}"

USER_NAME="$(id -un)"

# ---------- Logging ----------
say()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap] FAIL: %s\033[0m\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
skip() { printf '\033[2m  ↩ skip: %s\033[0m\n' "$*"; }

# ---------- Preflight ----------
step "Preflight"
[[ "$(uname)" == "Darwin" ]] || die "This script targets macOS; got $(uname)"
arch="$(uname -m)"
[[ "$arch" == "arm64" ]] || warn "Non-Apple-Silicon arch ($arch). Brew prefix logic assumes /opt/homebrew."
ok "macOS $arch"

# ---------- Xcode Command Line Tools ----------
step "Xcode Command Line Tools"
if xcode-select -p >/dev/null 2>&1; then
  skip "CLT already installed at $(xcode-select -p)"
else
  say "Triggering CLT install (a GUI dialog will pop up)"
  xcode-select --install || true
  warn "Wait for the CLT dialog to finish, then re-run this script."
  exit 1
fi

# ---------- Homebrew ----------
step "Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "brew at $(command -v brew)"
else
  say "Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -d /opt/homebrew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

BREW_PREFIX="$(brew --prefix)"
ok "BREW_PREFIX=$BREW_PREFIX"

# ---------- Brew packages ----------
step "Brew packages"
brew_install() {
  local formula="$1"
  if brew list --formula "$formula" >/dev/null 2>&1; then
    skip "$formula"
  else
    say "brew install $formula"
    brew install "$formula"
  fi
}
brew_install "uv"
brew_install "node@${XENON_NODE_VERSION}"
brew_install "postgresql@${XENON_PG_VERSION}"
brew_install "git"
brew_install "gh"

# Link node@N if not already on PATH as `node`
if ! command -v node >/dev/null 2>&1; then
  say "Linking node@${XENON_NODE_VERSION}"
  brew link --force --overwrite "node@${XENON_NODE_VERSION}"
fi
ok "node $(node --version)"
ok "uv $(uv --version)"

# ---------- Postgres service ----------
step "Postgres ${XENON_PG_VERSION} service"
PG_BIN="${BREW_PREFIX}/opt/postgresql@${XENON_PG_VERSION}/bin"
export PATH="${PG_BIN}:$PATH"

if brew services list | awk '{print $1, $2}' | grep -q "^postgresql@${XENON_PG_VERSION} started$"; then
  skip "postgresql@${XENON_PG_VERSION} already running"
else
  say "Starting postgresql@${XENON_PG_VERSION}"
  brew services start "postgresql@${XENON_PG_VERSION}"
  sleep 3
fi

# Wait for socket up to ~30s
for i in {1..30}; do
  if "${PG_BIN}/pg_isready" -h localhost >/dev/null 2>&1; then break; fi
  sleep 1
done
"${PG_BIN}/pg_isready" -h localhost >/dev/null 2>&1 || die "Postgres not responding on localhost after 30s"
ok "Postgres up"

# ---------- DB role + database ----------
step "Database role + DB"
PSQL="${PG_BIN}/psql -h localhost -U ${USER_NAME} postgres"

if $PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='${XENON_DB_ROLE}'" | grep -q 1; then
  skip "role ${XENON_DB_ROLE} exists"
else
  say "Creating role ${XENON_DB_ROLE}"
  read -r -s -p "  Password for new ${XENON_DB_ROLE} role: " PG_PW; echo
  $PSQL -c "CREATE ROLE ${XENON_DB_ROLE} LOGIN PASSWORD '${PG_PW}';"
  ok "role created"
fi

if $PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='${XENON_DB_NAME}'" | grep -q 1; then
  skip "database ${XENON_DB_NAME} exists"
else
  say "Creating database ${XENON_DB_NAME}"
  $PSQL -c "CREATE DATABASE ${XENON_DB_NAME} OWNER ${XENON_DB_ROLE};"
  ok "database created"
fi

# ---------- Repo clone ----------
step "Repo at ${XENON_HOME}"
if [[ -d "${XENON_HOME}/.git" ]]; then
  skip "repo already cloned"
  (cd "${XENON_HOME}" && git fetch --tags origin && git checkout "${XENON_BRANCH}" && git pull --ff-only)
else
  mkdir -p "$(dirname "${XENON_HOME}")"
  say "Cloning ${XENON_REPO}"
  if ! git clone "${XENON_REPO}" "${XENON_HOME}"; then
    warn "Clone failed. Likely no GitHub SSH key. Run: ssh-keygen -t ed25519 -C \"$USER_NAME@macmini\""
    warn "Then add the public key at https://github.com/settings/keys and re-run this script."
    exit 1
  fi
  (cd "${XENON_HOME}" && git checkout "${XENON_BRANCH}")
fi
cd "${XENON_HOME}"
ok "repo at $(git rev-parse --short HEAD) on $(git symbolic-ref --short HEAD || echo detached)"

# ---------- .env scaffolding ----------
step ".env files"
if [[ ! -f "${XENON_HOME}/.env" ]]; then
  say "Creating .env from .env.example (you must fill secrets before services start)"
  cp "${XENON_HOME}/.env.example" "${XENON_HOME}/.env"
  # Pre-fill the values we DO know
  python3 - <<PY
from pathlib import Path
import secrets
p = Path("${XENON_HOME}/.env")
text = p.read_text()
db_url = "postgresql+psycopg://${XENON_DB_ROLE}@localhost:5432/${XENON_DB_NAME}"
db_url_test = "postgresql+psycopg://${XENON_DB_ROLE}@localhost:5432/${XENON_DB_NAME}_test"
quote_secret = secrets.token_hex(32)
extras = []
if "DATABASE_URL=" not in text:
    extras.append(f"DATABASE_URL={db_url}")
if "DATABASE_URL_TEST=" not in text:
    extras.append(f"DATABASE_URL_TEST={db_url_test}")
text = text.replace("XENON_QUOTE_TOKEN_SECRET=", f"XENON_QUOTE_TOKEN_SECRET={quote_secret}")
text = text.replace("XENON_TRADING_MODE=paper", f"XENON_TRADING_MODE=${XENON_TRADING_MODE}")
if extras:
    text = text.rstrip() + "\n\n# Bootstrap-injected\n" + "\n".join(extras) + "\n"
p.write_text(text)
print("  .env scaffolded")
PY
  warn "OPEN ${XENON_HOME}/.env AND FILL: MENTHORQ_USER, MENTHORQ_PASS, MASSIVE_API_KEY,"
  warn "                                 CLERK_JWKS_URL, CLERK_ISSUER, ALLOWED_USER_IDS,"
  warn "                                 R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"
else
  skip ".env exists (not overwriting)"
fi

if [[ ! -f "${XENON_HOME}/web/.env" ]]; then
  say "Creating web/.env shell"
  cat > "${XENON_HOME}/web/.env" <<'EOF'
# Fill these before npm run build
ANTHROPIC_API_KEY=
UW_TOKEN=
EXA_API_KEY=
CEREBRAS_API_KEY=
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
CLERK_SECRET_KEY=
EOF
  warn "OPEN ${XENON_HOME}/web/.env AND FILL all values."
else
  skip "web/.env exists"
fi

# ---------- uv sync ----------
step "uv sync (Python deps)"
(cd "${XENON_HOME}" && uv sync --frozen --extra test)
ok "Python deps synced"

# ---------- Alembic ----------
step "Alembic schema"
# shellcheck disable=SC1091
set -a; source "${XENON_HOME}/.env"; set +a
if [[ -z "${DATABASE_URL:-}" ]]; then
  die "DATABASE_URL missing from .env — fill it before continuing"
fi
(cd "${XENON_HOME}" && uv run alembic upgrade head)
ok "schema at head"

# ---------- npm install + build ----------
step "Web build"
(cd "${XENON_HOME}" && npm install --no-audit --no-fund --legacy-peer-deps)
(cd "${XENON_HOME}/web" && npm install --no-audit --no-fund --legacy-peer-deps)
(cd "${XENON_HOME}/web" && npm run build)
ok "web built"

# ---------- launchd plists ----------
step "Render + install launchd plists"
mkdir -p "${XENON_HOME}/logs" "$HOME/Library/LaunchAgents"

UV_BIN="$(command -v uv)"
NODE_BIN="$(command -v node)"
NPM_BIN="$(command -v npm)"

render_plist() {
  local label="$1"
  local src="${XENON_HOME}/config/templates/${label}.plist.template"
  local dst="$HOME/Library/LaunchAgents/${label}.plist"
  [[ -f "$src" ]] || die "missing template: $src"
  sed \
    -e "s|__PROJECT_DIR__|${XENON_HOME}|g" \
    -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__BREW_PREFIX__|${BREW_PREFIX}|g" \
    -e "s|__UV_BIN__|${UV_BIN}|g" \
    -e "s|__NODE_BIN__|${NODE_BIN}|g" \
    -e "s|__NPM_BIN__|${NPM_BIN}|g" \
    -e "s|__TRADING_MODE__|${XENON_TRADING_MODE}|g" \
    "$src" > "$dst"
  ok "rendered $dst"
}

render_plist "com.xenon.api"
render_plist "com.xenon.web"
render_plist "com.xenon.ib-realtime"

for label in com.xenon.api com.xenon.web com.xenon.ib-realtime; do
  plist="$HOME/Library/LaunchAgents/${label}.plist"
  # Bootstrap unloads (if loaded) then loads. Idempotent.
  launchctl unload "$plist" >/dev/null 2>&1 || true
  launchctl load "$plist"
  ok "loaded $label"
done

# ---------- Health checks ----------
step "Health checks"
# Give services a moment to come up
sleep 5

check_url() {
  local url="$1" name="$2"
  for i in {1..20}; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      ok "$name reachable: $url"
      return 0
    fi
    sleep 1
  done
  warn "$name NOT reachable at $url after 20s — check logs/${name}.err.log"
  return 1
}

api_ok=0; web_ok=0
check_url "http://127.0.0.1:8321/health" "api" && api_ok=1 || true
check_url "http://127.0.0.1:3000"         "web" && web_ok=1 || true

# ---------- Summary ----------
step "Bootstrap summary"
printf '  Repo:           %s\n' "${XENON_HOME}"
printf '  Trading mode:   %s\n' "${XENON_TRADING_MODE}"
printf '  Database:       %s @ %s\n' "${XENON_DB_NAME}" "localhost:5432"
printf '  API:            %s\n' "$([[ $api_ok == 1 ]] && echo UP || echo DOWN)"
printf '  Web:            %s\n' "$([[ $web_ok == 1 ]] && echo UP || echo DOWN)"
printf '  IB realtime:    loaded (verify with logs/ib-realtime.err.log)\n'

cat <<NEXT

Next steps (manual):
  1. Promote data from laptop:
     # on the LAPTOP:
     ./scripts/deploy/macmini-data-promote.sh <macmini-tailscale-host>

  2. Install IB Gateway + IBC for unattended sessions:
     ./scripts/infra/setup_ibc.sh

  3. Flip trading mode to live (when ready):
     # edit .env: XENON_TRADING_MODE=live
     # then: launchctl kickstart -k gui/\$UID/com.xenon.api

  4. Check ongoing health:
     curl http://127.0.0.1:8321/health
     tail -f logs/api.err.log

NEXT
[[ $api_ok == 1 && $web_ok == 1 ]] || exit 1
