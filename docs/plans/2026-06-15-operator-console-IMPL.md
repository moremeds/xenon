# Operator Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or `/execute-plan`) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Clerk-gated `/admin` "Operator" console that renders xenon's live IB/health/broker signals (Tier A, 11 tiles) plus a market-hours-aware writer-freshness table backed by a new `service_health` Postgres heartbeat (Tier B).

**Architecture:** A single FastAPI aggregate route `GET /admin/operator` reuses the existing `/health` helper functions and adds three new reads (IB-auth verdict, latest `uw_api_stats`, scope-filtered `service_health` rows + synthesized "missing" rows). Background loops record heartbeats into a new `xenon.service_health` table (scoped per `AccountScope`) via a best-effort `record_service_health()` helper that derives state from each loop's actual result. The frontend is a new `/admin` route rendering `<WorkspaceShell section="operator">` with the shell's broker-sync hooks **disabled** for the operator section (so the page is read-only — cached GET reads only), whose operator branch renders `<OperatorConsole>` (own polling hook → `/api/admin/operator`). The endpoint is auth-gated by the global `auth_middleware` (no per-route `Depends`, matching `/orders`); the heartbeat writer no-ops under `XENON_READ_ONLY`.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / Alembic / Postgres (`xenon` schema) · Next.js App Router / TypeScript / React · Vitest (jsdom) · chrome-cdp/Playwright E2E. All Python via `uv`.

**Source design:** `docs/plans/2026-06-15-radon-port-ideas.md` § Area 3. Brainstorm decisions: Tier A + B, Approach A (standalone route + aggregate endpoint). Tier C (controls), Tier D (off-box prober/SLO, systemd panel, host_metrics), and the 7d Uptime/MTTR event-history are **out of scope** (documented extension points).

---

## Hard constraints (verify each PR against these)

- **DB-first** — `service_health` is a Postgres table; never a JSON file. CI guard: `no_json_write_on_order_path.py` (operator paths are not order paths, but stay DB-first regardless).
- **Read-only surface** — `/admin/operator` performs **no** mutations. `record_service_health()` **no-ops under `XENON_READ_ONLY=1`**. The operator section also **disables the shell's sync hooks** (`usePortfolio`/`useFutuPortfolio`/`useOrders` gated off — Task 20 Step 4b) so opening `/admin` triggers zero broker-sync POSTs, only cached GET reads.
- **Auth** — page gated by the default-private Clerk middleware (`web/middleware.ts`). The endpoint carries **no per-route `Depends`** — it's gated by the global `auth_middleware` (`server.py:590`), exactly like `/orders` and every other data route (per-route `Depends` would 401 cross-container in Docker since the Next proxy forwards no token; see Task 9). `service_health` carries full `AccountScope` columns per the repo rule.
- **Brand** — 4px max radius, all colors via CSS tokens (no raw hex), no gradients/glass/soft shadows, mono for machine values. Reuse the dashboard's `snapshot-card` CSS language.
- **No `Math.abs` on prices/P&L** — N/A here (no prices rendered), but preserve any signed values verbatim.
- **TDD** — failing test → implement → green → commit. 95% coverage target. **Browser verification is mandatory** before "done."

---

## File Structure

### New — backend

| File                                                       | Responsibility                                                                                                                                            |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/db/service_health.py`                           | `record_service_health()` best-effort upsert helper. Importable by both api lifespan loops and execution CLIs (lives in `db/` to avoid circular imports). |
| `src/xenon/db/migrations/versions/<rev>_service_health.py` | Alembic migration creating `xenon.service_health` (down_revision = `2026_06_13_fill_qty_numeric`).                                                        |

### Modified — backend

| File                                                      | Change                                                                                                                                                                                                                           |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/db/schema.py`                                  | Add the `service_health` `Table` definition.                                                                                                                                                                                     |
| `src/xenon/api/server.py`                                 | Add `_ib_auth_verdict()`, `_uw_api_health()`, `_service_health_rows()` helpers + the `@app.get("/admin/operator")` route (inline, next to the other `_*_health` helpers — avoids circular import with a separate router module). |
| `src/xenon/api/services/ib_activity_mirror.py`            | Heartbeat per poller tick (`ib_activity_poller`, ok/error).                                                                                                                                                                      |
| `src/xenon/api/server.py` (boot tasks)                    | Heartbeat after fills-replay (`ib_fills_replay`) and rehydrate (`ib_rehydrate`).                                                                                                                                                 |
| `src/xenon/execution/naked_short_audit.py`                | Heartbeat at end of run (`naked_short_audit`).                                                                                                                                                                                   |
| Futu history loop (`_maybe_start_futu_history_loop` site) | Heartbeat per run (`futu_history`).                                                                                                                                                                                              |

### New — frontend

| File                                                  | Responsibility                                                                                         |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `web/app/admin/page.tsx`                              | Route entry → `<WorkspaceShell section="operator" />`.                                                 |
| `web/app/api/admin/operator/route.ts`                 | Next proxy → `xenonFetch("/admin/operator")`.                                                          |
| `web/lib/operatorTypes.ts`                            | `OperatorData` + sub-types (mirror the endpoint payload).                                              |
| `web/lib/serviceHealthWindows.ts`                     | `isWriterStale(service, ageSecs, market)` — market-hours-aware staleness. Pure, unit-tested.           |
| `web/components/operator/OperatorConsole.tsx`         | Top-level: polls `/api/admin/operator`, owns refresh state, renders header + tile grid + writer table. |
| `web/components/operator/SignalTile.tsx`              | Generic label/value/sub tile with tone.                                                                |
| `web/components/operator/IbGatewayCard.tsx`           | IB gateway status card.                                                                                |
| `web/components/operator/IbPoolRoles.tsx`             | sync/orders/data pool roles.                                                                           |
| `web/components/operator/WriterFreshnessTable.tsx`    | service_health rows w/ market-aware staleness.                                                         |
| `web/components/operator/ReliabilityRollupHeader.tsx` | Composite "● rollup · IB <verdict> · updated Ns ago".                                                  |

### Modified — frontend

| File                                | Change                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `web/lib/types.ts`                  | Add `"operator"` to `WorkspaceSection`.                                                                             |
| `web/lib/data.ts`                   | Add the "Operator" nav item (`href: "/admin"`, `route: "operator"`).                                                |
| `web/components/WorkspaceShell.tsx` | Add `"operator"` to the section exclusion lists (lines ~470, ~511, ~522) + an operator render branch near line 509. |
| ~~`web/lib/ibConnectionAlert.ts`~~  | **No change** — MFA push guidance already implemented (Pass-1 finding; see Task 21).                                |
| `web/app/globals.css`               | Operator tile/grid/table styles (reuse `snapshot-card` tokens).                                                     |

### New — tests

`src/xenon/api/tests/test_operator_endpoint.py`, `src/xenon/db/tests/test_service_health.py` (or `scripts/tests/`), and `web/tests/{serviceHealthWindows,signal-tile,ib-gateway-card,ib-pool-roles,writer-freshness-table,reliability-rollup-header,operator-console,connection-banner-mfa}.test.tsx`.

---

## Data contract (the single source of truth for all tasks)

`GET /admin/operator` returns:

```jsonc
{
  "generated_at": "2026-06-15T14:00:00Z",
  "ib_gateway":   { "port_listening": true, "upstream_dead": false, "service_state": "...", "host": "...", "port": 4001, "gateway_mode": "cloud" },
  "ib_pool":      { "sync": {"connected": true, "client_id": 11}, "orders": {...}, "data": {...} },
  "ib_auth":      "authenticated",            // authenticated | awaiting | unreachable | unknown
  "trading_mode": "paper",
  "account":      "DU***889",                 // masked
  "mode_verified": true,
  "snapshotter":  { "last_write_at": "...", "stale_seconds": 12 },
  "order_submissions": { "unknown_count": 0, "alarm": false },
  "flex_divergence":   { "configured": true, "ran_at": "...", "divergence_count": 0, "total_compared": 42 },
  "realtime_subscribers": { "reachable": true, "ib_connected": true, "subscribers": [], "anonymous_count": 0, "ttl_ms": 30000 },
  "futu":         { "configured": true, "connected": false, "last_sync_at": null, "last_sync_age_s": null },
  "uw":           { "bucket_hour": "...", "requests": 12, "cache_hits": 9, "status_2xx": 12, "status_4xx": 0, "status_5xx": 0, "latency_avg_ms": 145.0 } /* or null */,
  "writers":      [ { "service": "ib_activity_poller", "state": "ok", "detail": null, "last_error": null, "last_started_at": null, "last_finished_at": null, "updated_at": "...", "age_secs": 45 } ]
}
```

TypeScript `OperatorData` (Task 11) mirrors this exactly.

---

## MILESTONE 1 — `service_health` table + heartbeat helper

### Task 1: `service_health` schema + migration

**Files:**

- Modify: `src/xenon/db/schema.py`
- Create: `src/xenon/db/migrations/versions/<rev>_service_health.py` (via alembic)
- Test: `scripts/tests/test_service_health_migration.py`

- [ ] **Step 1: Add the Table to `schema.py`**

Find the `XENON_SCHEMA`/`xenon_metadata` definitions and the existing `Table(...)` blocks (e.g. `uw_api_stats` at ~line 1130). Add near the other operational tables:

```python
service_health = Table(
    "service_health",
    xenon_metadata,
    # AccountScope columns — REQUIRED by the repo's scope rule. Without them the
    # nightly core_dev→core_test refresh copies live (prod) heartbeats into the
    # dev DB where paper-session heartbeats share the same `service` key → they
    # collide/overwrite. Composite PK keeps live vs paper rows distinct.
    Column("service", Text, nullable=False),
    Column("broker", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("detail", Text),
    Column("last_error", Text),
    Column("last_started_at", TIMESTAMP(timezone=True)),
    Column("last_finished_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("service", "broker", "account_env", "broker_account",
                         name="pk_service_health"),
)
```

(Confirm `Text`, `Column`, `TIMESTAMP`, `text`, `PrimaryKeyConstraint` are imported at the top of `schema.py` — the first four are used by existing tables; add `PrimaryKeyConstraint` to the SQLAlchemy import if absent.)

- [ ] **Step 2: Generate the migration**

```bash
uv run alembic revision -m "service_health table"
```

This auto-sets `down_revision` to the current head (`2026_06_13_fill_qty_numeric`). Open the new file and fill the bodies:

```python
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.create_table(
        "service_health",
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("service", "broker", "account_env", "broker_account",
                                name="pk_service_health"),
        schema="xenon",
    )

def downgrade() -> None:
    op.drop_table("service_health", schema="xenon")
```

> **Test-DB note (Pass-1 finding):** pytest worker DBs are **cloned via `CREATE DATABASE … TEMPLATE`** (`src/xenon/_test_db.py:427`), so the new table must exist in the template/master test DB before xdist clones inherit it. After `alembic upgrade head` against your dev DB, also run it against `DATABASE_URL_TEST`'s master test DB, and drop any stale `xenon_test_gwN` clones so the fixture recreates them from the migrated template. For local verification run the migration test serially first (`-n0`), then `-n auto`.

- [ ] **Step 3: Write the failing test**

`scripts/tests/test_service_health_migration.py`:

```python
import sqlalchemy as sa
from xenon.db.engine import get_sync_engine

def test_service_health_table_exists(pg_test_engine):
    insp = sa.inspect(get_sync_engine())
    cols = {c["name"] for c in insp.get_columns("service_health", schema="xenon")}
    assert {"service", "broker", "account_env", "broker_account", "state",
            "detail", "last_error", "last_started_at", "last_finished_at",
            "updated_at"} <= cols
    pk = set(insp.get_pk_constraint("service_health", schema="xenon")["constrained_columns"])
    assert pk == {"service", "broker", "account_env", "broker_account"}
```

- [ ] **Step 4: Apply migration + run test**

```bash
uv run alembic upgrade head
uv run pytest scripts/tests/test_service_health_migration.py -xvs
```

Expected: PASS. (Per `CLAUDE.md`, the `pg_test_engine` fixture skips cleanly when Postgres is offline.)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/*service_health*.py scripts/tests/test_service_health_migration.py
git commit -m "feat(operator): add xenon.service_health heartbeat table"
```

### Task 2: `record_service_health()` helper

**Files:**

- Create: `src/xenon/db/service_health.py`
- Test: `scripts/tests/test_record_service_health.py`

- [ ] **Step 1: Write the failing test**

```python
import os
from datetime import datetime, timezone
import sqlalchemy as sa
from xenon.db.engine import get_sync_engine
from xenon.db.schema import service_health
from xenon.db.service_health import record_service_health

def _row(service):
    with get_sync_engine().connect() as c:
        return c.execute(sa.select(service_health).where(service_health.c.service == service)).mappings().first()

def test_insert_then_update(pg_test_engine):
    record_service_health("unit_test_writer", "ok")
    r = _row("unit_test_writer")
    assert r["state"] == "ok"
    record_service_health("unit_test_writer", "error", error={"msg": "boom"})
    r2 = _row("unit_test_writer")
    assert r2["state"] == "error"
    assert "boom" in (r2["last_error"] or "")

def test_read_only_noop(pg_test_engine, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    record_service_health("ro_writer", "ok")
    assert _row("ro_writer") is None

def test_never_raises(monkeypatch):
    # Force engine failure; helper must swallow it.
    monkeypatch.setenv("XENON_READ_ONLY", "0")
    import xenon.db.service_health as mod
    monkeypatch.setattr(mod, "get_sync_engine", lambda: (_ for _ in ()).throw(RuntimeError("no db")), raising=False)
    record_service_health("x", "ok")  # must not raise
```

- [ ] **Step 2: Run it (fails — module missing)**

```bash
uv run pytest scripts/tests/test_record_service_health.py -xvs
```

Expected: FAIL (`ModuleNotFoundError: xenon.db.service_health`).

- [ ] **Step 3: Implement the helper**

`src/xenon/db/service_health.py`:

```python
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Local import alias so monkeypatch in tests can swap the engine factory.
from xenon.db.engine import get_sync_engine  # noqa: E402


def _resolve_scope(broker, account_env, broker_account):
    """Resolve AccountScope columns: explicit args win; else read the env vars
    sync subprocesses already set (XENON_TRADING_MODE/XENON_BROKER_ACCOUNT,
    per src/xenon/CLAUDE.md § Broker Account Scope). Falls back to 'unknown'
    so a heartbeat is never silently dropped for a missing env."""
    return (
        broker or "IB",
        account_env or os.environ.get("XENON_TRADING_MODE") or "unknown",
        broker_account or os.environ.get("XENON_BROKER_ACCOUNT") or "unknown",
    )


def record_service_health(
    service: str,
    state: str = "ok",
    *,
    broker: Optional[str] = None,
    account_env: Optional[str] = None,
    broker_account: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    detail: Optional[str] = None,
    error: Optional[dict[str, Any] | str] = None,
) -> None:
    """Best-effort upsert of a per-(service, scope) heartbeat into
    ``xenon.service_health``.

    - Scope (broker/account_env/broker_account) is REQUIRED by the repo rule.
      Callers with an AccountScope pass it explicitly; subprocess CLIs let it
      resolve from XENON_TRADING_MODE/XENON_BROKER_ACCOUNT env (see _resolve_scope).
    - Never raises: a heartbeat failure must not break the caller's loop.
    - No-ops under ``XENON_READ_ONLY=1`` (mirrors ``_save_portfolio_to_postgres``).
    """
    if os.environ.get("XENON_READ_ONLY") == "1":
        return
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from xenon.db.schema import service_health

        brk, env, acct = _resolve_scope(broker, account_env, broker_account)
        now = datetime.now(timezone.utc)
        err_text = json.dumps(error) if isinstance(error, dict) else error
        values = dict(
            service=service,
            broker=brk,
            account_env=env,
            broker_account=acct,
            state=state,
            detail=detail,
            last_error=err_text,
            last_started_at=started_at,
            last_finished_at=finished_at,
            updated_at=now,
        )
        engine = get_sync_engine()
        with engine.begin() as conn:
            stmt = pg_insert(service_health).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[service_health.c.service, service_health.c.broker,
                                service_health.c.account_env, service_health.c.broker_account],
                set_={k: stmt.excluded[k] for k in values
                      if k not in ("service", "broker", "account_env", "broker_account")},
            )
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_service_health(%s) failed: %s", service, exc)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_record_service_health.py -xvs
```

Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/service_health.py scripts/tests/test_record_service_health.py
git commit -m "feat(operator): record_service_health best-effort heartbeat helper"
```

---

## MILESTONE 2 — Heartbeat wiring into background loops

### Task 3: Activity-poller heartbeat (the marquee writer)

**Files:**

- Modify: `src/xenon/api/services/ib_activity_mirror.py` (`activity_poller_loop`, ~line 352)
- Test: `scripts/tests/test_activity_poller_heartbeat.py`

- [ ] **Step 1: Write the failing test** (assert the loop records a heartbeat per tick)

```python
import asyncio
import pytest
import xenon.api.services.ib_activity_mirror as mod

@pytest.mark.asyncio
async def test_poller_records_heartbeat(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "record_service_health", lambda *a, **k: calls.append((a, k)), raising=False)

    async def fake_runner(fn, **kw):
        return {"open_orders": {}, "fills": {}, "cancel_sweep": {}}

    # Run one tick then cancel.
    task = asyncio.create_task(mod.activity_poller_loop(
        ib_client_factory=lambda: None,
        scope=_DummyScope(),
        interval_s=0.01,
        async_runner=fake_runner,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(a and a[0] == "ib_activity_poller" for a, _ in calls)
```

(Provide a minimal `_DummyScope` with `as_dict()` + `as_dict`-compatible attrs, matching `AccountScope` usage in the loop's `logger.info`.)

- [ ] **Step 2: Run it (fails — no heartbeat yet)**

```bash
uv run pytest scripts/tests/test_activity_poller_heartbeat.py -xvs
```

Expected: FAIL.

- [ ] **Step 3: Add the import + heartbeat calls**

At the top of `ib_activity_mirror.py` add:

```python
from xenon.db.service_health import record_service_health
```

Inside `activity_poller_loop`, after the per-tick `logger.info(...)` block (success path) add — passing the loop's `scope` explicitly (it's in scope as a param) and recording **liveness**:

```python
            record_service_health(
                "ib_activity_poller", "ok",
                broker=scope.broker, account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
```

And in the broad `except Exception as exc:` block (after `logger.exception(...)`, before the sleep) record the **real** exception (Gemini Pass-2: don't log a generic string):

```python
            record_service_health(
                "ib_activity_poller", "error", error={"msg": str(exc)},
                broker=scope.broker, account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
```

(Rename the broad handler to `except Exception as exc:` if it isn't already.) Do **not** add a heartbeat in the `except asyncio.CancelledError` branch (clean shutdown is not an error).

> **Heartbeat semantics (Codex Pass-2):** this `ok` means **liveness** — the poller loop completed a tick — which is the failure this table primarily makes visible (a hung/dead poller). Per-subtask failures inside a tick (`run_activity_poll_tick` catches them internally and returns counts) are already surfaced via `order_submissions.alarm` / `snapshotter.stale_seconds`. Document this in the function docstring so "ok" is not read as "every sub-sync succeeded." If `run_activity_poll_tick`'s returned dict later exposes explicit error markers, map them to `state="error"` here.

- [ ] **Step 4: Run test**

```bash
uv run pytest scripts/tests/test_activity_poller_heartbeat.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/ib_activity_mirror.py scripts/tests/test_activity_poller_heartbeat.py
git commit -m "feat(operator): heartbeat ib_activity_poller per tick"
```

### Task 4: Boot-task heartbeats (fills-replay + rehydrate)

**Files:**

- Modify: `src/xenon/api/server.py` (`_run_fills_replay_on_boot` ~line 344; the rehydrate-on-boot call site)

> **Codex Pass-2 (false-OK):** `reconcile_fills_on_boot()` returns `{"skipped": …}` / `{"error": …}` **without raising**, and `_run_rehydrate_on_boot()` swallows failures and returns `None`. Recording `"ok"` merely because the `await` returned would label skipped/failed boots as healthy. Map heartbeat state from the **actual result**, and emit the rehydrate heartbeat **inside** `_run_rehydrate_on_boot` where success/failure is known. Both boot helpers build an `AccountScope scope` — pass its fields.

- [ ] **Step 1: fills-replay heartbeat — derive state from the returned dict**

In `_run_fills_replay_on_boot`, capture the result of `await asyncio.wait_for(...)` and map it:

```python
        from xenon.db.service_health import record_service_health
        result = await asyncio.wait_for(ib_pool.run_sync("sync", reconcile_fills_on_boot, ...), timeout=30.0)
        state = "error" if (result or {}).get("error") else ("paused" if (result or {}).get("skipped") else "ok")
        record_service_health(
            "ib_fills_replay", state,
            error=(result or {}).get("error") and {"msg": str((result or {}).get("error"))},
            finished_at=datetime.now(timezone.utc),
            broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account,
        )
```

On the `except (asyncio.TimeoutError, Exception) as exc:` path add `record_service_health("ib_fills_replay", "error", error={"msg": str(exc)}, broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account)`.

- [ ] **Step 2: rehydrate heartbeat — emit inside `_run_rehydrate_on_boot`, keyed on real outcome**

`_run_rehydrate_on_boot` (verified `server.py:151-217`) has **two** independent try/except phases (single-leg `:182-197`, combo `:201-217`), each catching `TimeoutError`/`Exception` and continuing. Track a success flag per phase and record once at the end; `_scope_account_env`/`_scope_account` are already computed at `:174-175` (pass them — `None` falls back to env resolution):

```python
    # near :182, before the single-leg try:
    single_ok = combo_ok = False
    # ...inside the single-leg try, after wait_for succeeds (:193 area):
    single_ok = True
    # ...inside the combo try, after wait_for succeeds (:213 area):
    combo_ok = True
    # ...at the very end of the function:
    from xenon.db.service_health import record_service_health
    ok = single_ok and combo_ok
    record_service_health(
        "ib_rehydrate", "ok" if ok else "error",
        error=None if ok else {"msg": "rehydrate phase failed/timed out (see logs)"},
        finished_at=datetime.now(timezone.utc),
        broker="IB", account_env=_scope_account_env, broker_account=_scope_account,
    )
```

- [ ] **Step 3: Verify via existing lifespan/boot tests**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: PASS (no regression). Add a focused test asserting fills-replay records `state="paused"` when the helper returns `{"skipped": ...}` and `"error"` on `{"error": ...}` (stub `reconcile_fills_on_boot`), so the false-OK fix is locked in.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/api/server.py
git commit -m "feat(operator): heartbeat ib_fills_replay + ib_rehydrate on boot (state from result)"
```

### Task 5: CLI/loop heartbeats (naked_short_audit + futu_history)

**Files:**

- Modify: `src/xenon/execution/naked_short_audit.py` (`main(argv=None)`, ~line 320)
- Modify: `src/xenon/api/services/futu_history_scheduler.py` (`futu_history_loop` — the actual loop body; `_maybe_start_futu_history_loop` in `server.py:284` only starts it)

- [ ] **Step 1: naked_short_audit heartbeat — cover ALL success exits**

> **Codex Pass-2:** `main()` returns early in several _successful_ branches (dry-run, file-input, no-violations). A heartbeat literally at "the end of `main`" misses them. Emit it at the single point where the audit has finished its core work (after violations are computed + any cancellation done), **before** the branch-dependent returns; and emit `"error"` if the audit raised. Scope resolves from the CLI's `XENON_TRADING_MODE`/`XENON_BROKER_ACCOUNT` env (default path — no explicit scope needed). Ensure `from datetime import datetime, timezone` is imported.

**Verified control flow (`naked_short_audit.py:320-450`):** `main()` has several exits — the `--portfolio`/`--orders`/`--dry-run` branches (`:347-401`) are **forensic/test-only**, and the **default path** (`:403+`, pull orders from live IB + cancel) is the one the **post-sync prod orchestration always runs** (it passes no flags). Monitor the prod path: record the heartbeat at the end of the **default path**, just before its final `return summary`, deriving state from `summary` (which carries an `error` key on IB-connection failure):

```python
from xenon.db.service_health import record_service_health  # top of module
# ...default path, after `summary` is finalized, before its `return summary`:
record_service_health(
    "naked_short_audit",
    "error" if summary.get("error") else "ok",
    error={"msg": summary["error"]} if summary.get("error") else None,
    finished_at=datetime.now(timezone.utc),
)  # scope resolves from XENON_TRADING_MODE/XENON_BROKER_ACCOUNT env (CLI default path)
```

The forensic file/dry-run exits intentionally do **not** heartbeat — they're not the monitored writer. (`record_service_health` no-ops under `XENON_READ_ONLY`, so this is safe in read-only sessions.) Add `from datetime import datetime, timezone` if absent.

- [ ] **Step 2: futu_history heartbeat**

In `src/xenon/api/services/futu_history_scheduler.py::futu_history_loop`, after each successful `run_history_sync(...)` call, add (import `record_service_health` + `datetime/timezone` at the top of that module):

```python
        record_service_health("futu_history", "ok", finished_at=datetime.now(timezone.utc))
```

- [ ] **Step 3: Run affected tests**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/execution/naked_short_audit.py src/xenon/api/services/futu_history_scheduler.py
git commit -m "feat(operator): heartbeat naked_short_audit + futu_history"
```

---

## MILESTONE 3 — `GET /admin/operator` aggregate endpoint

### Task 6: `_ib_auth_verdict()` helper

**Files:**

- Modify: `src/xenon/api/server.py` (next to the other `_*_health` helpers, ~line 840–961)
- Test: `src/xenon/api/tests/test_operator_helpers.py`

- [ ] **Step 1: Write the failing test**

```python
from xenon.api.server import _ib_auth_verdict

def test_unreachable_when_port_closed():
    assert _ib_auth_verdict({"port_listening": False}, {}) == "unreachable"

def test_awaiting_when_upstream_dead():
    assert _ib_auth_verdict({"port_listening": True, "upstream_dead": True}, {}) == "awaiting"

def test_authenticated_when_any_role_connected():
    assert _ib_auth_verdict({"port_listening": True}, {"sync": {"connected": True}}) == "authenticated"

def test_unknown_when_listening_but_no_role():
    assert _ib_auth_verdict({"port_listening": True}, {"sync": {"connected": False}}) == "unknown"
```

- [ ] **Step 2: Run it (fails — not defined)** → `uv run pytest src/xenon/api/tests/test_operator_helpers.py -xvs`

- [ ] **Step 3: Implement**

```python
def _ib_auth_verdict(gw: dict, pool: dict) -> str:
    """Derive an IB auth verdict from gateway + pool status."""
    if not gw.get("port_listening"):
        return "unreachable"
    if gw.get("upstream_dead"):
        return "awaiting"
    any_connected = any((r or {}).get("connected") for r in pool.values()) if pool else False
    return "authenticated" if any_connected else "unknown"
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** → `git commit -m "feat(operator): _ib_auth_verdict helper"`

### Task 7: `_uw_api_health()` helper

**Files:** Modify `src/xenon/api/server.py`; Test `src/xenon/api/tests/test_operator_helpers.py` (append).

- [ ] **Step 1: Write failing test** (seed one `uw_api_stats` row, assert shape + averaged latency; empty → `None`):

```python
import sqlalchemy as sa
from decimal import Decimal
from datetime import datetime, timezone
from xenon.db.engine import get_sync_engine
from xenon.db.schema import uw_api_stats
from xenon.api.server import _uw_api_health

def test_uw_health_latest_row(pg_test_engine):
    with get_sync_engine().begin() as c:
        c.execute(sa.insert(uw_api_stats).values(
            bucket_hour=datetime(2026, 6, 15, 14, tzinfo=timezone.utc),
            requests=10, cache_hits=4, latency_sum=Decimal("300"), latency_count=3,
            status_2xx=10, status_4xx=0, status_5xx=0))
    h = _uw_api_health()
    assert h["requests"] == 10 and h["latency_avg_ms"] == 100.0

def test_uw_health_empty(pg_test_engine):
    assert _uw_api_health() is None
```

- [ ] **Step 2: Run (fails)** → `-xvs`
- [ ] **Step 3: Implement** (place near `_snapshotter_health`; reuse module imports `select`, `get_sync_engine`, `uw_api_stats`, `_iso_datetime`):

```python
def _uw_api_health() -> dict | None:
    """Latest hourly uw_api_stats bucket, or None when no rows exist."""
    try:
        from xenon.db.schema import uw_api_stats
        engine = get_sync_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(uw_api_stats).order_by(uw_api_stats.c.bucket_hour.desc()).limit(1)
            ).mappings().first()
    except Exception:
        logger.warning("[operator] failed to load uw_api_stats", exc_info=True)
        return None
    if row is None:
        return None
    lat_count = int(row["latency_count"] or 0)
    lat_sum = float(row["latency_sum"] or 0)
    return {
        "bucket_hour": _iso_datetime(row["bucket_hour"]),
        "requests": int(row["requests"] or 0),
        "cache_hits": int(row["cache_hits"] or 0),
        "status_2xx": int(row["status_2xx"] or 0),
        "status_4xx": int(row["status_4xx"] or 0),
        "status_5xx": int(row["status_5xx"] or 0),
        "latency_avg_ms": round(lat_sum / lat_count, 1) if lat_count else None,
    }
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): _uw_api_health helper"`

### Task 8: `_service_health_rows()` reader

**Files:** Modify `src/xenon/api/server.py`; Test append to `src/xenon/api/tests/test_operator_helpers.py` (must live here, not `scripts/tests/`: the api/tests autouse fixture sets `XENON_TRADING_MODE=paper` + `XENON_BROKER_ACCOUNT=DU0000000` and seeds `app.state`, so the scope `record_service_health` writes (resolved from env) matches the scope `_service_health_rows` filters by (resolved from `app.state` via `_resolve_scope_kwargs`). In `scripts/tests/` those env vars aren't set and the rows would be filtered out).

**Two requirements this reader must satisfy (Codex/Gemini Pass-2):**

1. **Scope-filter** rows to the operator's resolved `AccountScope` (so the dev DB, which after the nightly refresh holds both live and paper rows, shows only the active env's writers). Resolve scope the same way `_flex_divergence_health` does (`_resolve_scope_kwargs()` from `app.state`).
2. **Synthesize "missing" rows** for expected writers that have no row yet — otherwise a writer that never started is invisible (the rollup would read `0/0 fresh`). Negative-space observability.

- [ ] **Step 1: Write failing test** (assert exact rows + names + the missing-writer synthesis + naive-tz handling; do NOT just check "age_secs key exists" — that passes on an empty list):

```python
import sqlalchemy as sa
from datetime import datetime, timezone, timedelta
from xenon.db.engine import get_sync_engine
from xenon.db.schema import service_health
from xenon.db.service_health import record_service_health
from xenon.api.server import _service_health_rows, EXPECTED_WRITERS

def test_service_health_rows(pg_test_engine):
    record_service_health("ib_activity_poller", "ok")
    record_service_health("naked_short_audit", "error", error={"m": 1})
    rows = _service_health_rows()
    by_name = {r["service"]: r for r in rows}
    # real rows present with computed age
    assert by_name["ib_activity_poller"]["state"] == "ok"
    assert isinstance(by_name["ib_activity_poller"]["age_secs"], int)
    assert by_name["naked_short_audit"]["state"] == "error"
    # every expected writer appears (missing ones synthesized)
    assert set(EXPECTED_WRITERS) <= set(by_name)
    missing = [r for r in rows if r["state"] == "missing"]
    assert all(m["age_secs"] is None for m in missing)
    # sorted by service
    names = [r["service"] for r in rows]
    assert names == sorted(names)

def test_service_health_rows_handles_naive_timestamp(pg_test_engine):
    # Insert a NAIVE updated_at directly to verify tz-correction → finite age.
    naive = datetime.utcnow() - timedelta(seconds=120)
    with get_sync_engine().begin() as c:
        c.execute(sa.insert(service_health).values(
            service="ib_activity_poller", broker="IB",
            account_env="paper", broker_account="DU0000000",
            state="ok", updated_at=naive))
    row = next(r for r in _service_health_rows() if r["service"] == "ib_activity_poller")
    assert row["age_secs"] is not None and row["age_secs"] >= 0
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement** (add `EXPECTED_WRITERS` near the helper):

```python
EXPECTED_WRITERS = (
    "ib_activity_poller", "ib_fills_replay", "ib_rehydrate",
    "futu_history", "naked_short_audit",
)

def _service_health_rows() -> list[dict]:
    """service_health rows for the active scope, sorted by service, with
    age_secs + synthesized 'missing' rows for expected writers with no row."""
    try:
        from xenon.db.schema import service_health
        kwargs = _resolve_scope_kwargs()  # {broker, account_env, broker_account}
        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(service_health)
                .where(
                    service_health.c.broker == kwargs["broker"],
                    service_health.c.account_env == kwargs["account_env"],
                    service_health.c.broker_account == kwargs["broker_account"],
                )
                .order_by(service_health.c.service)
            ).mappings().all()
    except Exception:
        logger.warning("[operator] failed to load service_health", exc_info=True)
        rows = []
    now = datetime.now(timezone.utc)
    out = []
    seen = set()
    for r in rows:
        seen.add(r["service"])
        updated = r["updated_at"]
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        out.append({
            "service": r["service"],
            "state": r["state"],
            "detail": r["detail"],
            "last_error": r["last_error"],
            "last_started_at": _iso_datetime(r["last_started_at"]),
            "last_finished_at": _iso_datetime(r["last_finished_at"]),
            "updated_at": _iso_datetime(updated),
            "age_secs": int((now - updated).total_seconds()) if updated else None,
        })
    # Synthesize missing expected writers.
    for svc in EXPECTED_WRITERS:
        if svc not in seen:
            out.append({
                "service": svc, "state": "missing", "detail": None,
                "last_error": None, "last_started_at": None,
                "last_finished_at": None, "updated_at": None, "age_secs": None,
            })
    out.sort(key=lambda r: r["service"])
    return out
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): _service_health_rows reader (scope-filtered + missing synthesis)"`

### Task 9: `GET /admin/operator` route

**Files:** Modify `src/xenon/api/server.py` (add route right after the `/health` handler); Test `src/xenon/api/tests/test_operator_endpoint.py`.

- [ ] **Step 1: Write the failing test** (use the FastAPI test harness; pre-seed `app.state` per the autouse conftest — `TestClient(app)` without `with` skips lifespan):

```python
from fastapi.testclient import TestClient
from xenon.api import server as server_mod

def test_operator_payload_shape(pg_test_engine):
    # No per-route Depends; the global auth_middleware passes through in tests
    # (CLERK_JWKS_URL unset) — same as every other data-route test
    # (test_orders_routes_failures.py). No token/override needed.
    r = TestClient(server_mod.app).get("/admin/operator")
    assert r.status_code == 200
    body = r.json()
    for key in ("generated_at", "ib_gateway", "ib_pool", "ib_auth", "trading_mode",
                "snapshotter", "order_submissions", "flex_divergence",
                "realtime_subscribers", "futu", "uw", "writers"):
        assert key in body
    assert body["ib_auth"] in {"authenticated", "awaiting", "unreachable", "unknown"}
    # Nested-shape contract asserts (not just top-level keys):
    assert isinstance(body["ib_gateway"]["port_listening"], bool)
    assert ("stale_seconds" in body["snapshotter"])
    assert isinstance(body["order_submissions"]["alarm"], bool)
    assert isinstance(body["writers"], list)
```

> **Auth model (Codex Pass-2 finding):** Do **not** add `Depends(verify_clerk_jwt)` to this route. xenon data routes (`/orders`, `/journal`, …) carry **no per-route Depends** — auth is enforced by the global `auth_middleware` (`server.py:590`), which (a) is exempt for `AUTH_EXEMPT_PATHS`, (b) passes through when `CLERK_JWKS_URL` is unset, (c) localhost-bypasses Next→FastAPI SSR, else (d) requires a Clerk JWT. The Next proxy forwards **no** token (parity with `/api/orders`). Adding `Depends` here would 401 in Docker prod (web→api is cross-container, non-localhost, no Bearer) — exactly the topology trap in memory `verify_prod_docker_topology`. `/admin/operator` is protected by the global middleware (same as every data route) **plus** the Clerk-gated `/admin` page.

- [ ] **Step 2: Run (fails — 404)** → `uv run pytest src/xenon/api/tests/test_operator_endpoint.py -xvs`

- [ ] **Step 3: Implement the route** (immediately after `@app.get("/health")`):

```python
@app.get("/admin/operator")
async def admin_operator():  # no per-route Depends — see Auth model note below
    gw = await check_ib_gateway()
    pool = ib_pool.status() if ib_pool else {}
    return {
        "generated_at": _iso_datetime(datetime.now(timezone.utc)),
        "ib_gateway": gw,
        "ib_pool": pool,
        "ib_auth": _ib_auth_verdict(gw, pool),
        "trading_mode": getattr(app.state, "trading_mode", trading_mode.MODE),
        "account": mask_account(getattr(app.state, "account", "")),
        "mode_verified": getattr(app.state, "mode_verified", False),
        "snapshotter": _snapshotter_health(),
        "order_submissions": _order_submissions_health(),
        "flex_divergence": _flex_divergence_health(),
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
        "futu": _compute_futu_health(),
        "uw": _uw_api_health(),
        "writers": _service_health_rows(),
    }
```

(`check_ib_gateway`, `ib_pool`, `mask_account`, `trading_mode`, `_compute_futu_health`, `_iso_datetime`, `asyncio` are all already imported/defined in `server.py` — confirmed from the `/health` handler. No `Depends`/`verify_clerk_jwt` needed.)

**Why the DB helpers (`_uw_api_health`, `_service_health_rows`) are called synchronously, not via `asyncio.to_thread`:** this matches `/health` (which calls `_snapshotter_health()` etc. inline and only `to_thread`s the _network_ call `_realtime_subscribers_health`). Under the Phase-2 pytest fixture the sync engine is bound to the test's single transaction/connection; reading it from a `to_thread` worker thread would touch that connection cross-thread and break. Local PG reads are fast; keep them inline.

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** → `git commit -m "feat(operator): GET /admin/operator aggregate endpoint"`

### Task 10: Next proxy route

**Files:** Create `web/app/api/admin/operator/route.ts`; Test `web/tests/operator-route.test.ts` (optional — mirrors existing route tests if present, else skip per repo convention).

- [ ] **Step 1: Implement** (copy `web/app/api/health/route.ts` shape):

```typescript
import { NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  try {
    const data = await xenonFetch<Record<string, unknown>>("/admin/operator", {
      method: "GET",
      timeout: 8_000,
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof XenonApiError) {
      return NextResponse.json({ error: err.detail }, { status: err.status });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Unknown error" },
      { status: 502 },
    );
  }
}
```

- [ ] **Step 2: Typecheck** → `cd web && npm run typecheck` → PASS.
- [ ] **Step 3: Commit** → `git commit -m "feat(operator): next proxy route /api/admin/operator"`

---

## MILESTONE 4 — Frontend types, staleness, components

### Task 11: `operatorTypes.ts`

**Files:** Create `web/lib/operatorTypes.ts`.

- [ ] **Step 1: Implement** (mirror the data contract):

```typescript
export type IbAuthVerdict =
  | "authenticated"
  | "awaiting"
  | "unreachable"
  | "unknown";

export type IbGatewayInfo = {
  port_listening: boolean;
  upstream_dead?: boolean;
  service_state?: string;
  host?: string;
  port?: number;
  gateway_mode?: string;
};

export type IbPoolRole = { connected: boolean; client_id?: number };

export type SnapshotterInfo = {
  last_write_at: string | null;
  stale_seconds: number | null;
};
export type OrderSubmissionsInfo = {
  unknown_count: number | null;
  alarm: boolean;
};
export type FlexDivergenceInfo = {
  configured: boolean;
  ran_at?: string | null;
  divergence_count?: number | null;
  total_compared?: number | null;
};
export type RealtimeSubscribersInfo = {
  reachable: boolean;
  ib_connected?: boolean | null;
  subscribers: unknown[];
  anonymous_count: number;
  ttl_ms?: number | null;
};
export type FutuInfo = {
  configured: boolean;
  connected: boolean;
  last_sync_at?: string | null;
  last_sync_age_s?: number | null;
};
export type UwInfo = {
  bucket_hour: string;
  requests: number;
  cache_hits: number;
  status_2xx: number;
  status_4xx: number;
  status_5xx: number;
  latency_avg_ms: number | null;
} | null;

export type WriterRow = {
  service: string;
  state: string;
  detail: string | null;
  last_error: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  updated_at: string | null;
  age_secs: number | null;
};

export type OperatorData = {
  generated_at: string;
  ib_gateway: IbGatewayInfo;
  ib_pool: Record<string, IbPoolRole>;
  ib_auth: IbAuthVerdict;
  trading_mode: string;
  account: string;
  mode_verified: boolean;
  snapshotter: SnapshotterInfo;
  order_submissions: OrderSubmissionsInfo;
  flex_divergence: FlexDivergenceInfo;
  realtime_subscribers: RealtimeSubscribersInfo;
  futu: FutuInfo;
  uw: UwInfo;
  writers: WriterRow[];
};
```

- [ ] **Step 2: Typecheck** → `cd web && npm run typecheck` → PASS. **Step 3: Commit** → `git commit -m "feat(operator): operator payload types"`

### Task 12: `serviceHealthWindows.ts` (market-aware staleness)

**Files:** Create `web/lib/serviceHealthWindows.ts`; Test `web/tests/serviceHealthWindows.test.ts`.

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect } from "vitest";
import { isWriterStale } from "@/lib/serviceHealthWindows";
import { MarketState } from "@/lib/useMarketHours";

describe("isWriterStale", () => {
  it("poller fresh within RTH window", () => {
    expect(isWriterStale("ib_activity_poller", 60, MarketState.OPEN)).toBe(
      false,
    );
  });
  it("poller stale past RTH window", () => {
    expect(isWriterStale("ib_activity_poller", 600, MarketState.OPEN)).toBe(
      true,
    );
  });
  it("boot-only writer never stale", () => {
    expect(isWriterStale("ib_fills_replay", 999999, MarketState.OPEN)).toBe(
      false,
    );
  });
  it("null age (never reported) is stale", () => {
    expect(isWriterStale("ib_activity_poller", null, MarketState.OPEN)).toBe(
      true,
    );
  });
  it("unknown service uses default window", () => {
    expect(isWriterStale("mystery", 1000, MarketState.OPEN)).toBe(true);
  });
});
```

- [ ] **Step 2: Run (fails)**

```bash
cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts tests/serviceHealthWindows.test.ts
```

Expected: FAIL.

- [ ] **Step 3: Implement**

```typescript
import { MarketState } from "@/lib/useMarketHours";

export type StalenessWindow = {
  open: number | null;
  extended: number | null;
  closed: number | null;
};

// Max age (seconds) before a writer is "stale", per market state.
// null = not expected to run in that state → never stale.
export const SERVICE_WINDOWS: Record<string, StalenessWindow> = {
  ib_activity_poller: { open: 180, extended: 300, closed: 900 },
  ib_fills_replay: { open: null, extended: null, closed: null },
  ib_rehydrate: { open: null, extended: null, closed: null },
  futu_history: { open: null, extended: null, closed: null },
  naked_short_audit: { open: 3600, extended: 3600, closed: null },
};

const DEFAULT_WINDOW: StalenessWindow = {
  open: 300,
  extended: 600,
  closed: null,
};

export function isWriterStale(
  service: string,
  ageSecs: number | null,
  market: MarketState,
): boolean {
  if (ageSecs == null) return true;
  const w = SERVICE_WINDOWS[service] ?? DEFAULT_WINDOW;
  const limit =
    market === MarketState.OPEN
      ? w.open
      : market === MarketState.EXTENDED
        ? w.extended
        : w.closed;
  if (limit == null) return false;
  return ageSecs > limit;
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): market-aware writer staleness windows"`

### Task 13: `SignalTile` component

**Files:** Create `web/components/operator/SignalTile.tsx`; Test `web/tests/signal-tile.test.tsx`.

- [ ] **Step 1: Write the failing test**

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SignalTile } from "@/components/operator/SignalTile";

afterEach(() => cleanup());

describe("SignalTile", () => {
  it("renders label, value, sub", () => {
    render(<SignalTile label="Snapshotter" value="12s" sub="fresh" tone="core" />);
    expect(screen.getByText("Snapshotter")).toBeTruthy();
    expect(screen.getByText("12s")).toBeTruthy();
    expect(screen.getByText("fresh")).toBeTruthy();
  });
  it("applies tone class", () => {
    const { container } = render(<SignalTile label="x" value="y" tone="fault" />);
    expect(container.querySelector(".operator-tile__value--fault")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run (fails)** → `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts tests/signal-tile.test.tsx`

- [ ] **Step 3: Implement**

```tsx
import type { ReactNode } from "react";

export type SignalTone = "core" | "fault" | "warn" | "neutral";

export function SignalTile({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: SignalTone;
}) {
  return (
    <div className="operator-tile">
      <span className="operator-tile__label">{label}</span>
      <span className={`operator-tile__value operator-tile__value--${tone}`}>
        {value}
      </span>
      {sub != null ? <span className="operator-tile__sub">{sub}</span> : null}
    </div>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): SignalTile component"`

### Task 14: `IbGatewayCard` component

**Files:** Create `web/components/operator/IbGatewayCard.tsx`; Test `web/tests/ib-gateway-card.test.tsx`.

- [ ] **Step 1: Write the failing test** — render from an `OperatorData`-ish fixture, assert host:port + verdict text appear; assert "Unreachable" tone when `port_listening:false`.

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { IbGatewayCard } from "@/components/operator/IbGatewayCard";

afterEach(() => cleanup());

const GW = { port_listening: true, host: "100.66.147.98", port: 4001, gateway_mode: "cloud" };

describe("IbGatewayCard", () => {
  it("shows host:port and authenticated verdict", () => {
    render(<IbGatewayCard gateway={GW} verdict="authenticated" account="DU***889" tradingMode="paper" modeVerified />);
    expect(screen.getByText(/100\.66\.147\.98:4001/)).toBeTruthy();
    expect(screen.getByText(/authenticated/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement** (reuse `.snapshot-card`, `.panel-eyebrow`, `.panel-title`, `.panel-edge-trace`):

```tsx
import type { IbGatewayInfo, IbAuthVerdict } from "@/lib/operatorTypes";

const VERDICT_TONE: Record<IbAuthVerdict, string> = {
  authenticated: "core",
  awaiting: "warn",
  unreachable: "fault",
  unknown: "neutral",
};

export function IbGatewayCard({
  gateway,
  verdict,
  account,
  tradingMode,
  modeVerified,
}: {
  gateway: IbGatewayInfo;
  verdict: IbAuthVerdict;
  account: string;
  tradingMode: string;
  modeVerified: boolean;
}) {
  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">IB Gateway</p>
        <h3 className="panel-title">Gateway</h3>
        <span
          className={`operator-pill operator-pill--${VERDICT_TONE[verdict]}`}
        >
          {verdict}
        </span>
      </header>
      <dl className="operator-kv">
        <div>
          <dt>Host</dt>
          <dd>{`${gateway.host ?? "---"}:${gateway.port ?? "---"}`}</dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>{gateway.gateway_mode ?? "---"}</dd>
        </div>
        <div>
          <dt>Port</dt>
          <dd>{gateway.port_listening ? "listening" : "closed"}</dd>
        </div>
        <div>
          <dt>Account</dt>
          <dd>{account || "---"}</dd>
        </div>
        <div>
          <dt>Trading mode</dt>
          <dd>
            {tradingMode} {modeVerified ? "✓" : "⚠"}
          </dd>
        </div>
      </dl>
    </section>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): IbGatewayCard"`

### Task 15: `IbPoolRoles` component

**Files:** Create `web/components/operator/IbPoolRoles.tsx`; Test `web/tests/ib-pool-roles.test.tsx`.

- [ ] **Step 1: Failing test** — render `{sync:{connected:true,client_id:11}, orders:{connected:false}}`, assert role names + a connected/down dot.

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { IbPoolRoles } from "@/components/operator/IbPoolRoles";
afterEach(() => cleanup());
describe("IbPoolRoles", () => {
  it("renders each role with a status dot", () => {
    const { container } = render(<IbPoolRoles pool={{ sync: { connected: true, client_id: 11 }, orders: { connected: false } }} />);
    expect(screen.getByText("sync")).toBeTruthy();
    expect(screen.getByText("orders")).toBeTruthy();
    expect(container.querySelectorAll(".operator-pool__dot").length).toBe(2);
  });
});
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement**:

```tsx
import type { IbPoolRole } from "@/lib/operatorTypes";

export function IbPoolRoles({ pool }: { pool: Record<string, IbPoolRole> }) {
  const roles = Object.entries(pool);
  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">IB Pool</p>
        <h3 className="panel-title">Roles</h3>
      </header>
      <ul className="operator-pool">
        {roles.length === 0 ? (
          <li className="operator-pool__empty">no pool</li>
        ) : null}
        {roles.map(([role, info]) => (
          <li key={role} className="operator-pool__row">
            <span
              className={`operator-pool__dot operator-pool__dot--${info?.connected ? "ok" : "down"}`}
              aria-hidden
            />
            <span className="operator-pool__role">{role}</span>
            <span className="operator-pool__cid">
              {info?.client_id != null ? `#${info.client_id}` : "---"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): IbPoolRoles"`

### Task 16: `WriterFreshnessTable` component

**Files:** Create `web/components/operator/WriterFreshnessTable.tsx`; Test `web/tests/writer-freshness-table.test.tsx`.

- [ ] **Step 1: Failing test** — fresh row (small age) renders "fresh"; stale row (large age, RTH) renders "STALE"; empty list → empty state. Inject `market` prop to keep the test deterministic (don't depend on wall-clock market state).

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { WriterFreshnessTable } from "@/components/operator/WriterFreshnessTable";
import { MarketState } from "@/lib/useMarketHours";
import type { WriterRow } from "@/lib/operatorTypes";

afterEach(() => cleanup());

const row = (over: Partial<WriterRow>): WriterRow => ({
  service: "ib_activity_poller", state: "ok", detail: null, last_error: null,
  last_started_at: null, last_finished_at: null, updated_at: "2026-06-15T14:00:00Z",
  age_secs: 30, ...over,
});

describe("WriterFreshnessTable", () => {
  it("marks fresh and stale rows", () => {
    render(<WriterFreshnessTable writers={[row({ age_secs: 30 }), row({ service: "x", age_secs: 9999 })]} market={MarketState.OPEN} />);
    // Use EXACT badge strings, not /fresh/i — the "Freshness" header would
    // also match a /fresh/i regex and break getByText (multiple matches).
    expect(screen.getByText("fresh")).toBeTruthy();
    expect(screen.getByText("STALE")).toBeTruthy();
  });
  it("renders a synthesized missing writer as a fault row", () => {
    render(<WriterFreshnessTable writers={[row({ service: "ib_fills_replay", state: "missing", age_secs: null, updated_at: null })]} market={MarketState.OPEN} />);
    expect(screen.getByText("missing")).toBeTruthy();   // state pill
    expect(screen.getByText("STALE")).toBeTruthy();      // null age → stale
    expect(screen.getByText("never")).toBeTruthy();      // last-run label
  });
  it("shows empty state", () => {
    render(<WriterFreshnessTable writers={[]} market={MarketState.OPEN} />);
    expect(screen.getByText(/no writers reported/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement**:

```tsx
import type { WriterRow } from "@/lib/operatorTypes";
import { MarketState } from "@/lib/useMarketHours";
import { isWriterStale } from "@/lib/serviceHealthWindows";

function ago(secs: number | null): string {
  if (secs == null) return "never";
  if (secs < 90) return `${secs}s ago`;
  if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function WriterFreshnessTable({
  writers,
  market,
}: {
  writers: WriterRow[];
  market: MarketState;
}) {
  return (
    <section className="snapshot-card operator-writers">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Writer Freshness</p>
        <h3 className="panel-title">Background writers</h3>
      </header>
      {writers.length === 0 ? (
        <p className="operator-writers__empty">No writers reported.</p>
      ) : (
        <table className="operator-writers__table">
          <thead>
            <tr>
              <th>Writer</th>
              <th>State</th>
              <th>Freshness</th>
              <th>Last run</th>
            </tr>
          </thead>
          <tbody>
            {writers.map((w) => {
              const stale = isWriterStale(w.service, w.age_secs, market);
              const stateTone =
                w.state === "error" || w.state === "missing" ? "fault" : "ok";
              return (
                <tr key={w.service}>
                  <td className="operator-writers__name">{w.service}</td>
                  <td>
                    <span
                      className={`operator-pill operator-pill--${stateTone === "fault" ? "fault" : "core"}`}
                    >
                      {w.state}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`operator-writers__fresh operator-writers__fresh--${stale ? "stale" : "fresh"}`}
                    >
                      {stale ? "STALE" : "fresh"}
                    </span>
                  </td>
                  <td className="operator-writers__age">{ago(w.age_secs)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): WriterFreshnessTable"`

### Task 17: `ReliabilityRollupHeader` component

**Files:** Create `web/components/operator/ReliabilityRollupHeader.tsx`; Test `web/tests/reliability-rollup-header.test.tsx`.

- [ ] **Step 1: Failing test** — renders verdict + "updated Ns ago" given `generatedAt`/`updatedSecsAgo`; shows worst-writer summary.

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ReliabilityRollupHeader } from "@/components/operator/ReliabilityRollupHeader";

afterEach(() => cleanup());

describe("ReliabilityRollupHeader", () => {
  it("renders verdict + updated age + writer summary", () => {
    render(<ReliabilityRollupHeader verdict="authenticated" updatedSecsAgo={2} writerSummary="3 fresh" />);
    expect(screen.getByText(/authenticated/i)).toBeTruthy();
    expect(screen.getByText(/updated 2s ago/i)).toBeTruthy();
    expect(screen.getByText(/3 fresh/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement**:

```tsx
import type { IbAuthVerdict } from "@/lib/operatorTypes";

export function ReliabilityRollupHeader({
  verdict,
  updatedSecsAgo,
  writerSummary,
}: {
  verdict: IbAuthVerdict;
  updatedSecsAgo: number | null;
  writerSummary: string;
}) {
  const updated =
    updatedSecsAgo == null ? "updating…" : `updated ${updatedSecsAgo}s ago`;
  return (
    <div className="operator-rollup">
      <span className="operator-rollup__left">
        <span className="operator-rollup__summary">{writerSummary}</span>
        <span className="operator-rollup__sep">·</span>
        <span className="operator-rollup__verdict">IB {verdict}</span>
      </span>
      <span className="operator-rollup__updated">{updated}</span>
    </div>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): ReliabilityRollupHeader"`

### Task 18: `OperatorConsole` (polling + layout)

**Files:** Create `web/components/operator/OperatorConsole.tsx`; Test `web/tests/operator-console.test.tsx`.

- [ ] **Step 1: Write the failing test** — mock `global.fetch` to resolve an `OperatorData` fixture; assert tiles + writer table render; assert null/empty payload is graceful.

```typescript
/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import OperatorConsole from "@/components/operator/OperatorConsole";
import type { OperatorData } from "@/lib/operatorTypes";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const DATA: OperatorData = {
  generated_at: "2026-06-15T14:00:00Z",
  ib_gateway: { port_listening: true, host: "h", port: 4001, gateway_mode: "cloud" },
  ib_pool: { sync: { connected: true, client_id: 11 } },
  ib_auth: "authenticated",
  trading_mode: "paper", account: "DU***889", mode_verified: true,
  snapshotter: { last_write_at: "2026-06-15T13:59:00Z", stale_seconds: 12 },
  order_submissions: { unknown_count: 0, alarm: false },
  flex_divergence: { configured: true, ran_at: null, divergence_count: 0, total_compared: 0 },
  realtime_subscribers: { reachable: true, ib_connected: true, subscribers: [], anonymous_count: 0, ttl_ms: 30000 },
  futu: { configured: true, connected: false, last_sync_at: null, last_sync_age_s: null },
  uw: null,
  writers: [{ service: "ib_activity_poller", state: "ok", detail: null, last_error: null, last_started_at: null, last_finished_at: null, updated_at: "2026-06-15T13:59:30Z", age_secs: 30 }],
};

describe("OperatorConsole", () => {
  it("renders tiles + writer table from a fetch", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => DATA }));
    render(<OperatorConsole />);
    await waitFor(() => expect(screen.getByText(/ib_activity_poller/)).toBeTruthy());
    expect(screen.getByText("IB Gateway")).toBeTruthy();
  });
  it("shows a loading/empty state before data", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    render(<OperatorConsole />);
    expect(screen.getByText(/operator — loading/i)).toBeTruthy();
  });
  it("surfaces a fault instead of hanging on loading when the feed errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => ({}) }));
    render(<OperatorConsole />);
    await waitFor(() => expect(screen.getByText(/HTTP 502/)).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run (fails)** **Step 3: Implement**:

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { useMarketHours } from "@/lib/useMarketHours";
import { isWriterStale } from "@/lib/serviceHealthWindows";
import type { OperatorData } from "@/lib/operatorTypes";
import { ReliabilityRollupHeader } from "./ReliabilityRollupHeader";
import { IbGatewayCard } from "./IbGatewayCard";
import { IbPoolRoles } from "./IbPoolRoles";
import { SignalTile } from "./SignalTile";
import { WriterFreshnessTable } from "./WriterFreshnessTable";

const POLL_MS = 8_000;

export default function OperatorConsole() {
  const market = useMarketHours();
  const [data, setData] = useState<OperatorData | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, force] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/api/admin/operator", { cache: "no-store" });
        if (!res.ok) {
          if (alive) setError(`Operator feed error (HTTP ${res.status})`);
          return;
        }
        const json = (await res.json()) as OperatorData;
        if (alive) {
          setData(json);
          setFetchedAt(Date.now());
          setError(null);
        }
      } catch (e) {
        // keep last good data, but surface the failure so the first load
        // doesn't hang forever on a fault (Codex Pass-2).
        if (alive)
          setError(
            e instanceof Error ? e.message : "Operator feed unreachable",
          );
      }
    };
    load();
    timer.current = setInterval(load, POLL_MS);
    const tick = setInterval(() => force((n) => n + 1), 1_000); // refresh "Ns ago"
    return () => {
      alive = false;
      if (timer.current) clearInterval(timer.current);
      clearInterval(tick);
    };
  }, []);

  if (!data) {
    // No data yet: distinguish a hard fault from the initial load so the
    // page never hangs on "loading…" forever (Codex Pass-2).
    return (
      <div className="operator-surface operator-surface--loading">
        {error ? `Operator — ${error}` : "Operator — loading…"}
      </div>
    );
  }

  const updatedSecsAgo = fetchedAt
    ? Math.max(0, Math.round((Date.now() - fetchedAt) / 1000))
    : null;
  // Fresh = recent AND not erroring. A writer that errors every minute is
  // "recent" but NOT healthy (Codex Pass-2) — require state==="ok".
  const freshCount = data.writers.filter(
    (w) => w.state === "ok" && !isWriterStale(w.service, w.age_secs, market),
  ).length;
  const writerSummary = `${freshCount}/${data.writers.length} healthy`;

  const snapTone =
    data.snapshotter.stale_seconds != null &&
    data.snapshotter.stale_seconds > 1800
      ? "fault"
      : "core";
  const orderTone = data.order_submissions.alarm ? "fault" : "neutral";
  const flexTone =
    (data.flex_divergence.divergence_count ?? 0) > 0 ? "warn" : "neutral";

  return (
    <div className="operator-surface">
      <ReliabilityRollupHeader
        verdict={data.ib_auth}
        updatedSecsAgo={updatedSecsAgo}
        writerSummary={writerSummary}
      />
      <div className="operator-surface__cards">
        <IbGatewayCard
          gateway={data.ib_gateway}
          verdict={data.ib_auth}
          account={data.account}
          tradingMode={data.trading_mode}
          modeVerified={data.mode_verified}
        />
        <IbPoolRoles pool={data.ib_pool} />
      </div>
      <div className="operator-surface__grid">
        <SignalTile
          label="Snapshotter"
          value={
            data.snapshotter.stale_seconds != null
              ? `${data.snapshotter.stale_seconds}s`
              : "---"
          }
          sub="staleness"
          tone={snapTone}
        />
        <SignalTile
          label="Order Queue"
          value={data.order_submissions.unknown_count ?? "---"}
          sub={data.order_submissions.alarm ? "ALARM" : "unknown(1h)"}
          tone={orderTone}
        />
        <SignalTile
          label="Flex Divergence"
          value={data.flex_divergence.divergence_count ?? "---"}
          sub={`of ${data.flex_divergence.total_compared ?? "---"}`}
          tone={flexTone}
        />
        <SignalTile
          label="Realtime"
          value={
            data.realtime_subscribers.reachable
              ? data.realtime_subscribers.ib_connected
                ? "live"
                : "ib off"
              : "down"
          }
          sub={`${data.realtime_subscribers.anonymous_count} subs`}
          tone={data.realtime_subscribers.reachable ? "core" : "fault"}
        />
        <SignalTile
          label="Futu"
          value={
            data.futu.connected
              ? "connected"
              : data.futu.configured
                ? "idle"
                : "off"
          }
          sub={
            data.futu.last_sync_age_s != null
              ? `${data.futu.last_sync_age_s}s`
              : "—"
          }
          tone={data.futu.connected ? "core" : "neutral"}
        />
        <SignalTile
          label="UW API"
          value={data.uw ? `${data.uw.requests} req` : "no data"}
          sub={
            data.uw && data.uw.latency_avg_ms != null
              ? `${data.uw.latency_avg_ms}ms`
              : "—"
          }
          tone={data.uw && data.uw.status_5xx > 0 ? "fault" : "neutral"}
        />
      </div>
      <WriterFreshnessTable writers={data.writers} market={market} />
    </div>
  );
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** → `git commit -m "feat(operator): OperatorConsole polling surface"`

### Task 19: Operator CSS

**Files:** Modify `web/app/globals.css` (append a new operator block near the dashboard `snapshot-card` rules).

- [ ] **Step 1: Add styles** (tokens only, 4px radius, no gradients/glass/shadows):

```css
/* ─── Operator console ─────────────────────────────────────── */
.operator-surface {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.operator-surface--loading {
  color: var(--text-muted);
  font-family: var(--font-mono);
  padding: 24px;
}

.operator-rollup {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border: 1px solid var(--border-dim);
  border-radius: 4px;
  padding: 12px 16px;
  background: var(--bg-panel);
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}
.operator-rollup__verdict {
  color: var(--text-primary);
}
.operator-rollup__sep {
  margin: 0 8px;
  color: var(--text-muted);
}
.operator-rollup__updated {
  color: var(--text-muted);
}

.operator-surface__cards {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.operator-surface__grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.operator-tile {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--border-dim);
  border-radius: 4px;
  padding: 12px 14px;
  background: var(--bg-panel);
}
.operator-tile__label {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.operator-tile__value {
  font-family: var(--font-sans);
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.operator-tile__value--core {
  color: var(--signal-core);
}
.operator-tile__value--fault {
  color: var(--fault);
}
.operator-tile__value--warn {
  color: var(--warning);
}
.operator-tile__value--neutral {
  color: var(--text-primary);
}
.operator-tile__sub {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
}

.operator-pill {
  font-family: var(--font-mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-dim);
}
.operator-pill--core {
  color: var(--signal-core);
  border-color: var(--signal-core);
}
.operator-pill--fault {
  color: var(--fault);
  border-color: var(--fault);
}
.operator-pill--warn {
  color: var(--warning);
}
.operator-pill--neutral {
  color: var(--text-muted);
}

.operator-kv {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
  margin: 0;
}
.operator-kv dt {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.operator-kv dd {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-primary);
}

.operator-pool {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.operator-pool__row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-mono);
  font-size: 13px;
}
.operator-pool__dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
}
.operator-pool__dot--ok {
  background: var(--signal-core);
}
.operator-pool__dot--down {
  background: var(--fault);
}
.operator-pool__role {
  color: var(--text-primary);
}
.operator-pool__cid {
  color: var(--text-muted);
  margin-left: auto;
}
.operator-pool__empty {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

.operator-writers__table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono);
  font-size: 12px;
}
.operator-writers__table th {
  text-align: left;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 10px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-dim);
}
.operator-writers__table td {
  padding: 8px;
  border-bottom: 1px solid var(--border-dim);
  color: var(--text-secondary);
}
.operator-writers__name {
  color: var(--text-primary);
}
.operator-writers__fresh--fresh {
  color: var(--signal-core);
}
.operator-writers__fresh--stale {
  color: var(--fault);
}
.operator-writers__empty {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

@media (max-width: 900px) {
  .operator-surface__cards {
    grid-template-columns: 1fr;
  }
  .operator-surface__grid {
    grid-template-columns: 1fr 1fr;
  }
}
```

(Token verified: `--warning` (#f5a623 dark / #d4910a light) is defined in `globals.css`. Do **not** use `--signal-warn` / `--warn` — the former doesn't exist and the latter is only defined in the `brand/` HTML mockups, not in `globals.css`.)

- [ ] **Step 2: Typecheck/lint** → `cd web && npm run lint` → PASS (0 new errors).
- [ ] **Step 3: Commit** → `git commit -m "feat(operator): console styles (brand tokens)"`

---

## MILESTONE 5 — Routing, nav, page, banner

### Task 20: Wire the `/admin` route + nav + section

**Files:**

- Modify: `web/lib/types.ts` (WorkspaceSection)
- Modify: `web/lib/data.ts` (navItems)
- Create: `web/app/admin/page.tsx`
- Modify: `web/components/WorkspaceShell.tsx` (exclusions + branch)
- Test: `web/tests/operator-shell-wiring.test.tsx` (optional shell render smoke)

- [ ] **Step 1: `WorkspaceSection`** — add `"operator"`:

```typescript
export type WorkspaceSection =
  | "dashboard"
  | "portfolio"
  | "performance"
  | "orders"
  | "journal"
  | "operator"
  | "ticker-detail";
```

- [ ] **Step 2: Nav item** in `web/lib/data.ts` — add `Settings` (or `Activity`/`Radio`) to the existing `import { ... } from "lucide-react"` (lines 1–7) first, then add the nav entry:

```typescript
  { label: "Operator", route: "operator", href: "/admin", icon: Settings },
```

- [ ] **Step 3: Page** `web/app/admin/page.tsx`:

```tsx
import WorkspaceShell from "@/components/WorkspaceShell";

export default function AdminPage() {
  return <WorkspaceShell section="operator" />;
}
```

- [ ] **Step 4: WorkspaceShell** — import the console at top:

```typescript
import OperatorConsole from "@/components/operator/OperatorConsole";
```

Extend the three exclusion conditions (so operator shows neither the account tab bar, MetricCards, nor WorkspaceSections):

- Line ~470: `activeSection !== "dashboard" && activeSection !== "ticker-detail" && activeSection !== "operator"`
- Line ~511: same triple condition
- Line ~522: `activeSection !== "dashboard" && activeSection !== "operator"`

Add the operator branch right after the dashboard branch (after line ~509):

```tsx
{
  activeSection === "operator" ? <OperatorConsole /> : null;
}
```

- [ ] **Step 4b: Make the operator section READ-ONLY (Codex Pass-2 finding — CRITICAL).**

`WorkspaceShell` mounts `usePortfolio` / `useFutuPortfolio` / `useOrders`, which POST broker syncs (`/api/portfolio` POST, `/futu/sync` POST, `/orders/refresh` POST) when their `active`/`enabled` arg is true. Each does only a **cached GET read on mount** and POSTs **only** when the arg is true (verified: `usePortfolio.ts`, `useFutuPortfolio.ts:125-149`, `useOrders.ts:51-103`). So gate them off for the operator section, and hide the header sync button:

- `usePortfolio(isMarketActive)` → `usePortfolio(isMarketActive && activeSection !== "operator")` (line ~74)
- `useFutuPortfolio(isMarketActive)` → `useFutuPortfolio(isMarketActive && activeSection !== "operator")` (line ~75)
- `shouldAutoSyncOrders` → `(isOrdersPage || isMarketActive) && activeSection !== "operator"` (line ~111)
- Hide the header sync button when `activeSection === "operator"` (the sync control rendered ~line 443-457).

This makes the operator page genuinely read-only (only cached GET reads occur; zero sync POSTs) while keeping the shell's nav + `ConnectionBanner` (which already carries the MFA copy — Task 21).

- [ ] **Step 5: Typecheck + run web tests**

```bash
cd web && npm run typecheck && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts tests/operator-console.test.tsx tests/signal-tile.test.tsx tests/writer-freshness-table.test.tsx
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/lib/types.ts web/lib/data.ts web/app/admin/page.tsx web/components/WorkspaceShell.tsx
git commit -m "feat(operator): wire /admin route + Operator nav + shell branch"
```

### Task 21: ConnectionBanner 2FA-push guidance — ALREADY IMPLEMENTED (verify-only, no code)

**Pass-1 finding:** backlog #2's MFA guidance is **already shipped**. `web/lib/ibConnectionAlert.ts:22-27` already returns, on `ibIssue === "ibc_mfa_required"`:

```typescript
const DEFAULT_MFA_APPROVAL_MESSAGE =
  "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.";
// getConnectionBannerState():
if (input.ibIssue === "ibc_mfa_required") {
  return {
    tone: "warning",
    message: input.ibStatusMessage ?? DEFAULT_MFA_APPROVAL_MESSAGE,
  };
}
```

`ConnectionBanner.tsx` renders `banner.message`, and `WorkspaceShell` already feeds `ibIssue`. **No code change.** This task is dropped from the build to avoid re-implementing existing behavior (YAGNI). The Operator console inherits the same banner since it renders inside `WorkspaceShell`.

- [ ] **Step 1 (verify-only):** Confirm the banner still surfaces the phone-approval copy — covered by the E2E check in Task 22 (force/observe the `ibc_mfa_required` state, or assert the existing `getConnectionBannerState` unit behavior is intact via the existing suite). No new file, no commit.

---

## MILESTONE 6 — Full verification + E2E

### Task 22: Gates + E2E + leave stack up

- [ ] **Step 1: Full Python affected + web suites**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
cd web && npm run typecheck && npm run lint && npm test
```

Expected: all green; lint shows no NEW errors/warnings.

- [ ] **Step 2: Start the dev stack (paper) and leave it up**

```bash
scripts/infra/dev.sh paper
```

Wait for: Next on **3200**, FastAPI on **8421**, realtime on **8866**. Confirm `curl -s http://localhost:8421/health | jq .status` → `"ok"`.

- [ ] **Step 3: Seed visible writer rows** (so the freshness table isn't empty on a cold dev DB) — let the activity poller run ≥1 tick, or insert a row directly against `DATABASE_URL_PAPER` (the LOCAL `core_test`, per the two-core_test memory):

```bash
uv run python -c "from xenon.db.service_health import record_service_health as r; r('ib_activity_poller','ok'); r('futu_history','ok')"
```

- [ ] **Step 4: E2E — chrome-cdp (primary)** — navigate to `http://localhost:3200/admin` with auth bypass for E2E (`XENON_DISABLE_AUTH=1` is honored by middleware). Verify:
  - rollup header renders with an IB verdict + "updated Ns ago"
  - IB Gateway card + Pool roles render
  - the 6 signal tiles render
  - the Writer Freshness table lists ≥1 writer with a fresh/STALE badge
  - browser console has no errors (`list_console_messages`)
  - capture a screenshot artifact.

  Fallback: `cd web && npx playwright test` with a new `web/tests/e2e/operator.spec.ts` asserting the same. Confirm `web/playwright.config.ts` base URL / port.

- [ ] **Step 5: Leave the stack running** for user review. Report: the `/admin` URL, the ports, the screenshot path, and the exact verification evidence (curl output, console-clean confirmation, screenshot).

---

## Evidence checklist (fill during execution)

| Claim                                                   | Evidence                                                            | Re-verify                                                      |
| ------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------- |
| `service_health` table exists                           | `test_service_health_migration.py` PASS + `\d xenon.service_health` | `uv run pytest scripts/tests/test_service_health_migration.py` |
| heartbeat upsert works + read-only no-op + never raises | `test_record_service_health.py` 3 PASS                              | `uv run pytest scripts/tests/test_record_service_health.py`    |
| poller emits heartbeat                                  | `test_activity_poller_heartbeat.py` PASS                            | same                                                           |
| `/admin/operator` shape + auth                          | `test_operator_endpoint.py` PASS                                    | `uv run pytest src/xenon/api/tests/test_operator_endpoint.py`  |
| staleness math                                          | `serviceHealthWindows.test.ts` PASS                                 | vitest single-file                                             |
| all components render                                   | per-component vitest PASS                                           | `cd web && npm test`                                           |
| page renders in browser                                 | chrome-cdp screenshot + clean console                               | open `http://localhost:3200/admin`                             |

---

## Self-review notes (writing-plans gate)

- **Spec coverage:** Tier-A 11 tiles → Tasks 6–9 (endpoint) + 13–18 (UI). Writer freshness (Tier B) → Tasks 1–5 (table/heartbeats) + 16. Banner #2 → **already implemented** (Task 21 verify-only). Nav/route → Task 20. ✅
- **No placeholders:** every code step has concrete code. Verified against source: `--warning` token exists; DB helpers called inline (match `/health` + Phase-2 fixture); heartbeat sites pinned to verified line refs (`_run_rehydrate_on_boot:151-217`, `naked_short_audit.main:320-450`, `activity_poller_loop`, `futu_history_loop`). ✅
- **Type consistency:** `OperatorData` (Task 11) ↔ endpoint payload (Task 9) ↔ component props (Tasks 13–18). `record_service_health(service, state, *, broker, account_env, broker_account, …)` stable across Tasks 2/3/4/5. `isWriterStale(service, ageSecs, market)` stable across Tasks 12/16/18. `EXPECTED_WRITERS` shared by Task 8 reader + tests. ✅
- **Pass-2 (codex tribunal) applied:** (1) endpoint has **no** per-route `Depends` — gated by global `auth_middleware` like all data routes (per-route Depends would 401 cross-container); (2) `service_health` carries **AccountScope** composite PK (nightly refresh blend); (3) operator section **disables shell sync hooks** (read-only); (4) heartbeats **derive state from each loop's result** (no false-OK); (5) **missing writers synthesized** (`EXPECTED_WRITERS`); (6) rollup counts **healthy** (state==ok) not just recent; (7) first-load failure shows a **fault**, not an infinite spinner; (8) stronger tests (exact rows/names, naive-tz, nested shape, 502 fault, exact-string `getByText`). Dismissed: Gemini stale-`market` closure (calc is in render body) + `order_submissions` non-null (except path returns null). ✅
- **Out of scope (documented):** Tier C controls, Tier D (off-box prober/SLO, systemd panel, host_metrics), 7d Uptime/MTTR event history. ✅
