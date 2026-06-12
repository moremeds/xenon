# Handover — system consistency / mode-debug tool (brainstorming, paused)

> **For the next session:** read this file end-to-end before touching anything. It is self-contained — you do not need the prior conversation.

## What was happening

We were doing two things back-to-back:

1. A **read-only review** of the database/IB connection strategy across modes (paper/live/dev/test). Findings captured below — no code changed.
2. Started a **`/superpowers:brainstorming` session** for a "debug the system to ensure everything is the same" tool. Got as far as asking the user the first scope question. **Awaiting answer (A / B / C).**

Nothing was written, nothing was committed. The only artifact from this session is this handover.

## Decision pending from the user

The brainstorming skill is paused on this question:

> What does "ensure everything is the same" mean to you?
>
> - **(A) Pre-flight validator** — a CLI script in `scripts/checks/` you run before `dev.sh` to assert all env vars, DB URLs, IB host/port, and account prefixes are internally consistent for the declared mode. Exits non-zero if anything is wrong. Can also run in CI.
> - **(B) Runtime consistency report** — extends or replaces `GET /health` to return a deeper consistency check (alembic drift, scope-column population, DB↔mode↔account agreement) you can `curl` while the stack is running.
> - **(C) Both** — pre-flight script for launch-time, plus a richer `/health` (or new `/debug/consistency`) endpoint for runtime inspection.

When the user answers, the next steps are (per the brainstorming skill):

1. Continue clarifying questions one at a time.
2. Propose 2–3 approaches with tradeoffs.
3. Present design sections, get approval per section.
4. Write spec to `docs/superpowers/specs/2026-05-10-system-consistency-debug-design.md`.
5. Hand off to `superpowers:writing-plans` for the implementation plan.

**Do not skip the brainstorming flow** — even if the user says "just build it", run the gate. The point of (A) vs (B) vs (C) is that they have very different blast radii.

## What was reviewed (the database strategy)

### Postgres databases on `192.168.50.47:5432`

| Database    | Purpose                            | Who writes                           |
| ----------- | ---------------------------------- | ------------------------------------ |
| `core`      | Production / live trading          | Production services on Mac mini only |
| `core_dev`  | Dev workspace — this Mac's default | All dev work, paper + live mode      |
| `core_test` | CI + local pytest                  | Test suite only                      |

Schemas inside each: `xenon`, `apex`, `events`. Roles (from migration handover): `xenon_app/xenon_dev`, `apex_app/apex_app`, admin `moremeds/moremeds`.

### Connection instances

`src/xenon/db/engine.py` exposes two engines, both built from `DATABASE_URL`:

| Engine            | Driver  | Used by                                                                  |
| ----------------- | ------- | ------------------------------------------------------------------------ |
| `_engine` (async) | asyncpg | FastAPI routes via `get_engine()` (after `init_engine()` in lifespan)    |
| `_sync_engine`    | psycopg | Sync subprocesses: `ib_sync`, `ib_execute`, `orders_store`, combo wizard |

`_normalize_pg_url()` rewrites the driver portion so the same `DATABASE_URL` works for both. Async pool: `pool_size=10`, `max_overflow=5`, `pool_pre_ping=True`, `pool_recycle=3600`. Sync engine lazy-initialized on first call.

### Mode-specific behavior (driven by `dev.sh paper|live`)

```
live mode  → DATABASE_URL stays as-is              → core_dev @ 192.168.50.47
           → IB host = .env IB_GATEWAY_HOST         → 192.168.50.47:4001
           → expected account prefix               → U… (not DU)

paper mode → DATABASE_URL = DATABASE_URL_PAPER     → core_dev @ 127.0.0.1
           → IB host = 127.0.0.1                   → 127.0.0.1:4002
           → expected account prefix               → DU…
           → Next.js PORT = 3001 (3000 collides)
```

Both modes write to the **same `core_dev` database**. Paper/live separation is via **scope columns** (`broker`, `account_env`, `broker_account`), not separate DBs/schemas. Enforcement: `AccountScope` (`src/xenon/execution/account_scope.py`) raises if `XENON_BROKER_ACCOUNT` prefix mismatches `XENON_TRADING_MODE`.

### Mode verification at lifespan start (`server.py:526`)

```python
account = await asyncio.to_thread(_get_managed_account_for_health)
verified = trading_mode.verify_account(account)
app.state.trading_mode = trading_mode.MODE
app.state.account = account
app.state.mode_verified = verified
```

Mismatch (e.g. `.env says live` but Gateway logged into a paper account) → `mode_verified=False`, order routes return 503 until aligned. Server still boots.

### What `/health` already returns (`server.py:1535`)

Trading mode, masked account, mode_verified, IB gateway reachable, IB pool status, UW token present, futu, snapshotter, order_submissions, flex_divergence, vcg_cri_loop. **What it does NOT cover:** alembic schema version vs DB head, env-var coherence (e.g. `DATABASE_URL` host vs IB host vs trading mode), `DATABASE_URL_PAPER` defined when off-LAN, scope columns actually populated on recent rows.

## Live findings worth acting on (separate from the design)

These came up during the review and are independently actionable:

1. **Alembic schema drift between `core_dev` and the local repo.**
   `uv run alembic current` (against the `.env` `DATABASE_URL`, i.e. `core_dev @ 192.168.50.47`) returned:

   ```
   ERROR  Can't locate revision identified by '821e494b1b71'
   FAILED: Can't locate revision identified by '821e494b1b71'
   ```

   The remote `core_dev` has a migration revision (`821e494b1b71`) that does **not** exist in `src/xenon/db/migrations/versions/`. Either the remote was upgraded with a branch that was never merged, or local lost a revision file. **Investigate before running any migration locally** — `alembic upgrade head` from this state is undefined behavior.
   - Suggested first probe: `psql -h 192.168.50.47 -U xenon_app core_dev -c "SELECT version_num FROM alembic_version;"` (need to be on LAN).
   - Cross-check: `git log --all --diff-filter=D -- 'src/xenon/db/migrations/versions/*821e494b1b71*'` to see if the file was deleted.

2. **`DATABASE_URL_PAPER` silently falls back.**
   `dev.sh paper` warns but continues if `DATABASE_URL_PAPER` is unset — paper mode then hangs on the remote LAN box if you're off-VPN. The `.env` currently has `DATABASE_URL_PAPER=postgresql+asyncpg://xenon_app:xenon_dev@127.0.0.1:5432/core_dev` set, so this is fine on this Mac, but a fresh checkout would fail. Worth either making `dev.sh` hard-fail or seeding `.env.example`.

3. **One Postgres role does everything.**
   `xenon_app` is the only writer credential. The strategy doc (`docs/architecture/production-database-strategy.md`) calls for `xenon_prod_app`, `xenon_readonly`, `signal_writer`, etc. — none exist. Out of scope for the current task; flagging because (B)/(C) above could verify role isolation if it ever lands.

## Files I read (in case you want to re-ground)

- `docs/architecture/production-database-strategy.md` — full target architecture (one production DB, schema split, scope columns, sync rules)
- `src/xenon/api/CLAUDE.md` — FastAPI/IB pool/lifespan policy
- `src/xenon/CLAUDE.md` — broader package policy (clients, scanners, scope rules)
- `src/xenon/db/engine.py` — async + sync engine factories
- `src/xenon/execution/account_scope.py` — `resolve_from_env()` / `resolve_from_app_state()`
- `src/xenon/api/trading_mode.py` — `MODE`, `EXPECTED_PORT`, `EXPECTED_PREFIX`, `verify_account`
- `scripts/infra/dev.sh` — the launcher that wires mode → IB host/port + DATABASE_URL swap + XENON_BROKER_ACCOUNT
- `.env` (do not commit) — current values
- `src/xenon/api/server.py` lifespan (lines 490–570) and `/health` (lines 1535–1576)

## What to do when this session resumes

1. **Wait for the user's A/B/C answer.** Do not assume.
2. Continue the brainstorming flow (one question at a time): success criteria, what counts as "consistent", whether to verify migrations, whether to also cover Futu/realtime relay, whether to compare against a snapshot or just internal coherence, when in CI vs local.
3. Once design is presented and approved, write the spec under `docs/superpowers/specs/2026-05-10-system-consistency-debug-design.md`, then invoke `superpowers:writing-plans`.
4. **Do not write code in the brainstorming session** — that violates the HARD-GATE in the skill.

## What NOT to do

- Don't run `alembic upgrade head` against `core_dev` until finding 1 above is resolved — that DB is one revision ahead of the local code in an unknown direction.
- Don't extend `/health` ad-hoc before the brainstorming reaches a design — option (B) might land on a separate `/debug/consistency` endpoint to keep the auth-exempt `/health` payload small.
- Don't touch the production `core` DB. The dev workspace never points at it.
- Don't push to master without a PR (per global CLAUDE.md).
