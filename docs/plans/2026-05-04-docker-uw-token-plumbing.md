# Docker compose: forward `UW_TOKEN` (and other web-side secrets) into the FastAPI container

**Date:** 2026-05-04
**Scope:** `docker-compose.yml`, one regression test, one comment update.
**Blast radius:** docker-compose dev/prod boot only. No application code changes.

## Symptom

When the stack is deployed via `docker compose up` on the Mac mini, every UW-backed
API endpoint fails. The user noticed it after a Mac mini deploy and suspected an
"endpoint URL" misconfiguration.

## Root cause

`UW_TOKEN` is, by project convention (root `CLAUDE.md` Credentials table), stored
**only** in `web/.env`. In `docker-compose.yml`:

| Service    | env_file     | Has UW_TOKEN? |
| ---------- | ------------ | ------------- |
| `web`      | `./web/.env` | yes           |
| `api`      | `./.env`     | **no**        |
| `realtime` | `./.env`     | no (n/a)      |
| `migrator` | `./.env`     | no (n/a)      |

The FastAPI container therefore boots without `UW_TOKEN` in env. The startup
hook in `src/xenon/api/server.py:547-549` notices this and emits
`"UW_TOKEN not set — UW-dependent endpoints will fail"`, then leaves
`_uw_client = None`. Every `/uw/*` route and every internal call site that
gates on `uw_available` returns 503 / errors.

**Why source-run FastAPI doesn't hit this bug:** `src/xenon/api/server.py:81-82`
already calls `load_dotenv(PROJECT_ROOT / ".env")` and
`load_dotenv(PROJECT_ROOT / "web" / ".env")` at import time. On a laptop the
source layout is intact, so the second `load_dotenv` finds `web/.env` and
populates `UW_TOKEN`. Inside the container, the api Dockerfile only copies
`src/` (not the repo's `web/` tree), so `PROJECT_ROOT / "web" / ".env"` does
not exist and that `load_dotenv` call is a silent no-op. Compose-injected env
is the only viable channel.

The URL is fine — it is hard-coded to `https://api.unusualwhales.com/api`
in `src/xenon/clients/uw_client.py:93` and `src/xenon/utils/uw_api.py:12`.
The user's "endpoint" guess pointed at the right config file (`docker-compose.yml`),
just the wrong variable.

## Fix

Add `./web/.env` as a second `env_file` for the `api` service. Docker Compose
supports a list of env files; later entries take precedence on conflict (we
verified there are no overlapping keys between root `.env` and `web/.env` per
the Credentials table).

Why this approach over the alternatives:

- **Add `UW_TOKEN: ${UW_TOKEN:-}` under `environment:`** — requires the operator
  to also export `UW_TOKEN` into the shell that runs `docker compose up`, or
  duplicate it into root `.env`. Brittle on a launchd-managed Mac mini host.
- **Move `UW_TOKEN` into root `.env`** — breaks the documented convention in
  `CLAUDE.md`, splits the source of truth between two files, and forces a
  parallel update to `web/.env.example`, `macmini-bootstrap.sh`, etc.
- **Add `web/.env` as a second `env_file`** ← chosen. Single source of truth
  preserved (`web/.env`), no operator burden, no convention change.

**Least-privilege tradeoff (knowingly accepted):** Forwarding the entire
`web/.env` file also pushes `EXA_API_KEY`, `CEREBRAS_API_KEY`, and
`CLERK_SECRET_KEY` into the api container. Today the Python code in
`src/xenon/` does not consume `EXA_API_KEY` or `CEREBRAS_API_KEY` (only
`ANTHROPIC_API_KEY` is used, by `menthorq_client.py` and
`fetch_menthorq_cta.py`), so those two are over-exposed. We accept this
because:

1. The api container is internal and reachable only from compose-network
   peers + host. It is not internet-facing.
2. The alternative (a per-key `environment:` block driven by Compose
   interpolation, e.g. `UW_TOKEN: ${UW_TOKEN:-}`) requires the operator to
   re-export those values into the shell that runs `docker compose up`. On a
   launchd-managed Mac mini that's brittle and silently breaks again the next
   time someone adds a key to `web/.env` without remembering to also wire it
   into `docker-compose.yml`.
3. It correctly fixes `ANTHROPIC_API_KEY` plumbing as a side benefit (same
   class of bug — without this, `menthorq_client.py` would also fail in the
   container).

We do **not** add `web/.env` to the `realtime` or `migrator` services — they
have no consumer of these secrets today, and adding env_files speculatively
violates the project rule against "design for hypothetical future requirements".

### Diff sketch

```yaml
api:
  ...
  # Order matters: ./web/.env is loaded *after* ./.env so its values take
  # precedence on conflict. `required: false` is defensive — it keeps the
  # api block parsing cleanly when web/.env is absent. (Note: the web
  # service still requires web/.env, so the FULL stack still needs the
  # file. macmini-bootstrap.sh always creates one. The FastAPI startup
  # hook at server.py:547-549 also warns gracefully on missing UW_TOKEN
  # so an empty web/.env is fine.)
  env_file:
    - ./.env
    - path: ./web/.env       # UW_TOKEN, ANTHROPIC_API_KEY — canonical home
      required: false        # per CLAUDE.md. Forwards a few unused web-side
                             # keys (EXA, CEREBRAS, CLERK_SECRET); accepted
                             # per plan.
```

## Regression test

Add `scripts/tests/test_docker_compose_env_plumbing.py`:

1. Read `docker-compose.yml` as text (no PyYAML — it's not in the project's
   declared test extras and we don't want to add a dep for one assertion).
2. Locate the `api:` service block and extract its `env_file:` list using a
   small text-based parser (the file's structure is stable and the parser
   only needs to recognize a leading-spaces indent and dash-prefixed list
   items).
3. Assert the api service's `env_file` is **exactly**
   `["./.env", "./web/.env"]` in that order. Order matters: Compose applies
   later files with higher precedence, so `./web/.env` second is the
   intentional precedence; reversing it would silently flip behavior if a
   conflicting key is added in the future.
4. Document why in the test docstring (link back to this plan + the
   `server.py:547-549` warning line).

The test is hermetic (file-only, no Docker daemon needed) and runs in the
existing `python-tests` CI job.

## Manual verification on Mac mini

After the change is deployed:

```bash
# from the repo root on the Mac mini
docker compose up -d --build api
docker compose exec api env | grep -E '^UW_TOKEN=' >/dev/null && echo "UW_TOKEN present" || echo "MISSING"
docker compose logs api 2>&1 | grep -i 'UW_TOKEN not set' && echo "STILL BROKEN" || echo "warning gone"
curl -fsS http://127.0.0.1:8321/uw-stats | jq '.daily.requests' >/dev/null && echo "uw endpoint live"
```

Expected: `UW_TOKEN present`, no startup warning, `/uw-stats` returns 200.

## Out of scope

- Auditing every other env var that _might_ be needed in the api container
  (e.g. `CEREBRAS_API_KEY`). The two-file forward fixes the class of bug.
- Adding `web/.env` to `realtime`/`migrator` — no consumer.
- Changing where `UW_TOKEN` is stored (root vs web/.env). The convention is
  documented and we're not relitigating it here.
- Refactoring `server.py` to fail-loud on missing `UW_TOKEN` (it already warns;
  raising would block boot for legitimate UW-less local dev sessions).
