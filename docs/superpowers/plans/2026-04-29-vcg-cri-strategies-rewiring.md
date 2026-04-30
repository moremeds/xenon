# VCG-R + CRI Strategies Rewiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing VCG-R and CRI scanners into the order-entry path as a risk-budget override (alerts + active throttling of user-initiated entries with per-trade override and full audit logging), per `docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md`.

**Architecture:** Phase 0 corrects persistence and plumbing prerequisites surfaced in spec audit (CRI now writes to `cri_series`; web `/api/regime` proxies FastAPI; scheduler uses `pg_try_advisory_lock`; notifications go through `xenon.db.events.emit()` outbox). Phase 1 adds a thin Postgres view (`regime_state`) over the latest VCG/CRI rows and a per-scope audit table (`regime_overrides`) keyed on `submission_id`. Phase 2 ships the Python classifier (`RegimeState`), `get_regime_state` Depends, and `GET /regime` for UI. Phase 3 ships `RegimeGate.veto`, wires it into order routes, and lands the CI guards locking the gate in. Phase 4 consolidates VCG and CRI scans into a single supervisor loop in the FastAPI lifespan. Phase 5 closes out docs and the backlog item.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy + Alembic, Postgres 15, Next.js 15, React 19, Vitest, Playwright, `uv`.

**Spec reference:** Each task cites the relevant section of `docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md` ("the spec" below). Read the spec section before implementing if anything is unclear — the plan is a sequence of bite-sized actions; the spec has the full design rationale.

---

## File Structure

### Created

| Path                                                                       | Purpose                                                                                                    |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `src/xenon/db/migrations/versions/<rev>_add_regime_state_and_overrides.py` | Alembic migration: `regime_state` view + `regime_overrides` table (deferred FK to `order_submissions`)     |
| `src/xenon/api/services/regime_state.py`                                   | `RegimeState` dataclass + classifier + `get_regime_state` FastAPI dep with TTL cache                       |
| `src/xenon/api/services/regime_gate.py`                                    | `GateResult`, `RegimeGate.veto`, `_is_hedge`, `_max_loss_usd`                                              |
| `src/xenon/api/services/advisory_lock.py`                                  | `pg_try_advisory_lock` async context manager (shared with UW-daily refactor)                               |
| `src/xenon/api/routes/regime.py`                                           | `GET /regime`, `GET /regime/overrides`                                                                     |
| `scripts/checks/order_path_regime_gate_called.py`                          | CI guard: gate must be called from every order entry point                                                 |
| `scripts/tests/test_regime_state_classifier.py`                            | Unit tests for the classifier (no DB)                                                                      |
| `scripts/tests/test_regime_gate.py`                                        | Unit tests for `RegimeGate.veto`, `_is_hedge`, `_max_loss_usd`                                             |
| `scripts/tests/test_regime_state_view.py`                                  | Integration test: view shape against fixture rows                                                          |
| `scripts/tests/test_regime_overrides_audit.py`                             | Integration test: audit insert + deferred FK                                                               |
| `scripts/tests/test_vcg_cri_scan_loop.py`                                  | Integration test: scheduler + outbox emit                                                                  |
| `scripts/tests/test_advisory_lock.py`                                      | Unit test: lock acquired/released, second-acquirer no-ops                                                  |
| `web/tests/order-place-regime-block.test.ts`                               | FastAPI harness test: 409 BLOCK behaviour                                                                  |
| `web/tests/order-place-regime-throttle.test.ts`                            | FastAPI harness test: 422 resize_required behaviour                                                        |
| `web/tests/order-place-regime-override.test.ts`                            | FastAPI harness test: override path + audit row                                                            |
| `web/e2e/regime-gate-flow.spec.ts`                                         | Playwright golden-path: BLOCK → modal → override → blotter tag                                             |
| `docs/plans/2026-04-29-vcg-cri-rewiring-audit.md`                          | Phase 0 audit findings (scheduler topology, in-process callers, hedge structures, web file-read consumers) |

### Modified

| Path                                               | Phase   | Why                                                                                                                                                                                                                                       |
| -------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/scanners/cri.py`                        | 0       | Emit boolean `crash_trigger.fired` + `cta.forced_reduction` alongside existing fields; add `persist(payload, *, conn)` helper                                                                                                             |
| `src/xenon/api/server.py`                          | 0, 3, 4 | Phase 0: wire CRI persist into `POST /regime/scan`; refactor UW-daily worker guard to use new advisory-lock helper. Phase 3: call `RegimeGate.veto` from `/orders/place`, `/orders/modify`. Phase 4: add `_vcg_cri_scan_loop` to lifespan |
| `src/xenon/api/guards.py`                          | 3       | Parameterize covered-call cover-ratio (currently hard-coded 1.0) to accept a ratio argument                                                                                                                                               |
| `src/xenon/db/schema.py`                           | 1       | Add SQLAlchemy reflection for `regime_overrides`                                                                                                                                                                                          |
| `src/xenon/execution/combo_wizard/session.py`      | 3       | Wizard combo-submit (`_orders_place_from_body` caller at line 327) calls the gate                                                                                                                                                         |
| `web/app/api/regime/route.ts`                      | 0       | Rewrite from file-reads (`data/cri.json`, `data/cri_scheduled`) to `xenonFetch('/regime')` proxy                                                                                                                                          |
| `web/components/RegimePanel.tsx`                   | 0, 2    | Phase 0: remove live client-side CRI recompute. Phase 2: render per-scanner tier strip + freshness + binding-side highlight                                                                                                               |
| `web/components/order-wizard/*`                    | 3       | Intercept 409 BLOCK (modal + override) and 422 resize_required (Trim-to-fit prompt)                                                                                                                                                       |
| `web/components/blotter/*`                         | 3       | Render "Overridden" tag joined on `regime_overrides.submission_id`                                                                                                                                                                        |
| `.github/workflows/ci.yml`                         | 3       | Wire two new guards into `order-path-guards` job                                                                                                                                                                                          |
| `scripts/checks/no_json_fallback_on_order_path.py` | 3       | Extend to check `web/app/api/regime/route.ts` does not call `readDataFile` / `readFile` / `JSON.parse(fs.readFileSync(...))`                                                                                                              |
| `CLAUDE.md`                                        | 5       | Update Order-Path Guards section + Startup Checklist (consolidated VCG/CRI loop)                                                                                                                                                          |
| `docs/todo-backlog.md`                             | 5       | Mark item §7 shipped, link to spec                                                                                                                                                                                                        |

---

## Phase 0 — Persistence + plumbing prerequisites

Spec: §8.0. Without this phase the rest of the plan operates on missing data.

### Task 0.1: Audit doc — scheduler, callers, hedge structures, web consumers

**Files:**

- Create: `docs/plans/2026-04-29-vcg-cri-rewiring-audit.md`

- [ ] **Step 1: Inventory the scheduler topology and order entry points**

Run:

```bash
grep -n "asyncio.create_task\|cri_scan\|vcg_scan\|@app.post(.*/orders\|submit_combo\|_orders_place_from_body\|_orders_modify_from_body\|ib_place_order" src/xenon/api/server.py src/xenon/execution/combo_wizard/session.py
```

Capture every match with file path + line number.

- [ ] **Step 2: Cross-reference hedge structures**

Read `docs/trading/options-structures.json` and `docs/trading/strategy-vcg.md` + `docs/trading/strategies.md` (Strategy 6 — CRI). Extract every structure listed as a hedge instrument for either strategy. The output is a Python list literal that `_is_hedge` will use later.

- [ ] **Step 3: Find consumers of `data/cri.json` and friends**

Run:

```bash
grep -rn "data/cri\.json\|data/cri_scheduled\|cri.json" web/ src/ scripts/ --include='*.ts' --include='*.tsx' --include='*.py' | grep -v node_modules | grep -v .next
```

Note every reader so the Phase 0 web rewrite knows what it might break.

- [ ] **Step 4: Write the audit doc**

Write findings into `docs/plans/2026-04-29-vcg-cri-rewiring-audit.md` with these sections:

- `## 1. Pre-existing scheduler` — what runs CRI today (or "nothing"), cadence, entry points.
- `## 2. Order entry-point allowlist` — bullet list of every (file, line, function) that can reach `ib_place_order`. Used as the integration checklist for Phase 3.
- `## 3. Canonical hedge structure set` — the structure names + symbol filters for `_is_hedge`. Drop straight into Task 3.2 input.
- `## 4. Stale `data/cri.json` consumers` — every reader of the file. Each entry says either "rewrite in Phase 0.7" or "read-only legacy, leave alone".
- `## 5. Open spec questions surfaced by the audit` — anything that needs a spec patch before Phase 1.

- [ ] **Step 5: Commit**

```bash
git add docs/plans/2026-04-29-vcg-cri-rewiring-audit.md
git commit -m "docs(vcg-cri): Phase 0 audit — scheduler, callers, hedges, file-read consumers"
```

### Task 0.2: CRI scanner emits boolean `fired` and `forced_reduction` fields

**Files:**

- Modify: `src/xenon/scanners/cri.py`
- Test: `scripts/tests/test_cri_scanner_output_fields.py`

Spec ref: §8.0 step 1; schema ref: `src/xenon/db/schema.py:325, 333`.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_cri_scanner_output_fields.py`:

```python
"""CRI scanner JSON must emit the boolean fields the schema generated
columns expect, alongside the existing numeric fields."""
import json
import subprocess


def test_cri_cli_emits_boolean_fired_and_forced_reduction(tmp_path):
    out = subprocess.run(
        ["uv", "run", "xenon-cri-scan", "--json"],
        capture_output=True, text=True, check=True,
    ).stdout
    payload = json.loads(out)

    crash_trigger = payload["crash_trigger"]
    assert "fired" in crash_trigger and isinstance(crash_trigger["fired"], bool), \
        "crash_trigger.fired must be present as a bool"
    # legacy field retained
    assert "triggered" in crash_trigger

    cta = payload["cta"]
    assert "forced_reduction" in cta and isinstance(cta["forced_reduction"], bool), \
        "cta.forced_reduction must be present as a bool"
    # legacy field retained
    assert "forced_reduction_pct" in cta
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_cri_scanner_output_fields.py -xvs
```

Expected: FAIL — assertion error on the missing `fired` / `forced_reduction` keys.

- [ ] **Step 3: Implement the field emission**

Open `src/xenon/scanners/cri.py`. Find where `crash_trigger` and `cta` dicts are built (search for `"triggered"` and `"forced_reduction_pct"`). Add the boolean fields:

```python
# In the crash_trigger payload construction:
crash_trigger = {
    "triggered": <existing value>,         # legacy — keep
    "fired": bool(<existing value>),       # new — drives schema generated column
    # ...other fields unchanged
}

# In the cta payload construction (around cri.py:777):
cta = {
    "forced_reduction_pct": forced_pct,    # legacy — keep
    "forced_reduction": forced_pct >= FORCED_REDUCTION_THRESHOLD,  # new
    # ...other fields unchanged
}
```

Use the existing thresholds for the boolean derivation; do not introduce new ones. If a constant `FORCED_REDUCTION_THRESHOLD` does not already exist, place a private module-level constant `_FORCED_REDUCTION_PCT_TRIGGER = 25.0` (matching the existing CTA documentation in `docs/trading/strategies.md`), and call this out in the commit message so the reviewer can sanity-check the threshold against the strategy doc.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest scripts/tests/test_cri_scanner_output_fields.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Run the wider scanner test suite to confirm no regression**

```bash
uv run pytest scripts/tests/ -k cri -xvs
```

Expected: all CRI tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/scanners/cri.py scripts/tests/test_cri_scanner_output_fields.py
git commit -m "feat(cri): emit boolean crash_trigger.fired and cta.forced_reduction fields"
```

### Task 0.3: CRI persistence helper `xenon.scanners.cri.persist()`

**Files:**

- Modify: `src/xenon/scanners/cri.py`
- Test: `scripts/tests/test_cri_persist.py`

Spec ref: §8.0 step 2; existing PG table at `src/xenon/db/schema.py:292`; idempotency on `recorded_date` generated column.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_cri_persist.py`:

```python
"""CRI persist() writes a row to cri_series and is idempotent on
the recorded_date generated column (one row per calendar day)."""
import json
import datetime as dt

import pytest
import sqlalchemy as sa

from xenon.db.schema import cri_series
from xenon.scanners.cri import persist


@pytest.fixture
def conn(test_engine):
    """test_engine is the project's existing pytest fixture for DATABASE_URL_TEST."""
    with test_engine.begin() as c:
        c.execute(sa.delete(cri_series))
        yield c
        c.execute(sa.delete(cri_series))


def _payload(date="2026-04-29", cri_score=42.0, fired=False):
    return {
        "date": date,
        "vix": 22.5, "vvix": 95.0, "spy": 510.0,
        "vix_5d_roc": 1.2, "vvix_vix_ratio": 4.2,
        "spx_100d_ma": 505.0, "spx_distance_pct": 1.0,
        "cor1m": 0.45, "cor1m_previous_close": 0.46,
        "cor1m_5d_change": -0.02, "realized_vol": 18.0,
        "cri": {"score": cri_score, "components": {}},
        "cta": {"exposure_pct": 70.0, "forced_reduction": False,
                "forced_reduction_pct": 0.0, "selling_usd_b": 0.0},
        "menthorq_cta": {"score": 0.0},
        "crash_trigger": {"triggered": fired, "fired": fired},
    }


def test_persist_inserts_row(conn):
    persist(_payload(), conn=conn)
    rows = conn.execute(sa.select(cri_series)).all()
    assert len(rows) == 1
    assert float(rows[0].cri_score) == 42.0


def test_persist_is_idempotent_on_recorded_date(conn):
    persist(_payload(date="2026-04-29", cri_score=42.0), conn=conn)
    persist(_payload(date="2026-04-29", cri_score=99.0), conn=conn)
    rows = conn.execute(sa.select(cri_series).order_by(cri_series.c.id)).all()
    assert len(rows) == 1, "ON CONFLICT should drop the second insert"
    # Original score retained — second insert is a no-op (DO NOTHING)
    assert float(rows[0].cri_score) == 42.0


def test_persist_separate_dates_create_separate_rows(conn):
    persist(_payload(date="2026-04-28"), conn=conn)
    persist(_payload(date="2026-04-29"), conn=conn)
    rows = conn.execute(sa.select(cri_series)).all()
    assert len(rows) == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_cri_persist.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'persist' from 'xenon.scanners.cri'`.

- [ ] **Step 3: Implement `persist`**

Append to `src/xenon/scanners/cri.py`:

```python
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import cri_series


def persist(payload: dict, *, conn) -> None:
    """Idempotently write a CRI scan payload to cri_series.

    The `recorded_date` generated column is computed from `payload['date']`;
    we use INSERT ... ON CONFLICT DO NOTHING on that derived column so two
    scans on the same calendar day are a no-op (matches the strategy's
    once-per-day cadence even when the scheduler ticks faster).
    """
    stmt = pg_insert(cri_series).values(
        cri_level=float(payload["cri"]["score"]),
        alert=bool(payload["crash_trigger"]["fired"]),
        payload=payload,
    )
    # cri_series has no unique constraint on recorded_date by default; we
    # rely on a partial unique index added by the Phase 1 migration. For
    # the Phase 0 helper, we additionally guard with a SELECT-then-skip
    # pattern that matches the same recorded_date.
    existing = conn.execute(
        sa.select(cri_series.c.id).where(
            sa.text("recorded_date = make_date("
                    "split_part(:d,'-',1)::int,"
                    "split_part(:d,'-',2)::int,"
                    "split_part(:d,'-',3)::int)")
        ).bindparams(d=payload["date"])
    ).first()
    if existing is not None:
        return
    conn.execute(stmt)
```

If the SELECT-then-skip pattern feels brittle, the alternative (cleaner) is to add a `UNIQUE (recorded_date)` constraint to `cri_series` in this same task as a small migration, then use `ON CONFLICT (recorded_date) DO NOTHING`. **Pick the constraint route** — it's one extra Alembic file, but it's the canonical pattern. Note this in the commit message.

- [ ] **Step 4: Add the unique-constraint migration**

```bash
uv run alembic revision -m "add unique recorded_date to cri_series"
```

In the new migration file:

```python
def upgrade():
    op.create_unique_constraint(
        "uq_cri_series_recorded_date", "cri_series", ["recorded_date"]
    )

def downgrade():
    op.drop_constraint("uq_cri_series_recorded_date", "cri_series", type_="unique")
```

Update `persist` to use the cleaner pattern:

```python
def persist(payload: dict, *, conn) -> None:
    stmt = pg_insert(cri_series).values(
        cri_level=float(payload["cri"]["score"]),
        alert=bool(payload["crash_trigger"]["fired"]),
        payload=payload,
    ).on_conflict_do_nothing(index_elements=[cri_series.c.recorded_date])
    conn.execute(stmt)
```

- [ ] **Step 5: Run migrations and the test**

```bash
uv run alembic upgrade head
uv run pytest scripts/tests/test_cri_persist.py -xvs
```

Expected: migrations clean, tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/scanners/cri.py src/xenon/db/migrations/versions/*_add_unique_recorded_date_to_cri_series.py scripts/tests/test_cri_persist.py
git commit -m "feat(cri): persist() helper writes cri_series rows idempotently per recorded_date"
```

### Task 0.4: Wire CRI persist into `POST /regime/scan`

**Files:**

- Modify: `src/xenon/api/server.py` (around line 572 — the existing `POST /regime/scan` handler)

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_cri_persist.py` or create `scripts/tests/test_regime_scan_route_persists.py`:

```python
"""POST /regime/scan persists a cri_series row in addition to its
existing data/cri.json archive behaviour."""
import sqlalchemy as sa
from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.db.schema import cri_series


def test_regime_scan_route_writes_cri_series(test_engine, monkeypatch):
    with test_engine.begin() as c:
        c.execute(sa.delete(cri_series))

    with TestClient(app) as client:
        # Test mode pre-seeds app.state per scripts/tests/conftest.py
        resp = client.post("/regime/scan")
        assert resp.status_code == 200

    with test_engine.begin() as c:
        rows = c.execute(sa.select(cri_series)).all()
        assert len(rows) == 1, "POST /regime/scan must persist a cri_series row"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_regime_scan_route_persists.py -xvs
```

Expected: FAIL — no row written.

- [ ] **Step 3: Wire `persist()` into the route**

In `src/xenon/api/server.py`, find the `POST /regime/scan` handler. After the CRI scan completes and the JSON payload is parsed (around line 572 — locate via the existing `data/cri.json` archive line), add a `persist` call inside the same DB session:

```python
from xenon.scanners.cri import persist as _persist_cri

# ... existing scan + parse ...
with engine.begin() as conn:
    _persist_cri(payload, conn=conn)
# ... existing archive-to-data/cri.json (keep for now; remove in a follow-up
#     once the Phase 0 web rewrite has shipped and no readers remain) ...
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest scripts/tests/test_regime_scan_route_persists.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Run order-route tests to confirm no collateral damage**

```bash
uv run pytest scripts/tests/test_orders_routes_failures.py src/xenon/api/tests/test_orders_routes_failures.py -xvs 2>/dev/null || \
uv run pytest src/xenon/api/tests/ -xvs
```

Expected: existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py scripts/tests/test_regime_scan_route_persists.py
git commit -m "feat(regime): POST /regime/scan persists cri_series rows in addition to file archive"
```

### Task 0.5: `pg_try_advisory_lock` async context manager

**Files:**

- Create: `src/xenon/api/services/advisory_lock.py`
- Test: `scripts/tests/test_advisory_lock.py`

Spec ref: §4.1, §8.0 step 3.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_advisory_lock.py`:

```python
"""pg_try_advisory_lock acquires a session-scoped lock and releases on exit.
Two concurrent acquirers — only the first wins."""
import asyncio
import sqlalchemy as sa
import pytest

from xenon.api.services.advisory_lock import pg_try_advisory_lock


_KEY = 0x7E57_C0DE  # arbitrary 32-bit


@pytest.mark.asyncio
async def test_first_acquirer_wins_second_returns_false(async_engine):
    """Two parallel context managers on different connections — only one
    holds the lock at a time."""
    async with pg_try_advisory_lock(_KEY, engine=async_engine) as got_a:
        assert got_a is True
        async with pg_try_advisory_lock(_KEY, engine=async_engine) as got_b:
            assert got_b is False, "second acquirer must not get the lock"


@pytest.mark.asyncio
async def test_lock_released_after_context_exits(async_engine):
    async with pg_try_advisory_lock(_KEY, engine=async_engine) as got_a:
        assert got_a is True
    # outside the context: a fresh acquirer should now win
    async with pg_try_advisory_lock(_KEY, engine=async_engine) as got_b:
        assert got_b is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_advisory_lock.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'pg_try_advisory_lock'`.

- [ ] **Step 3: Implement the helper**

Create `src/xenon/api/services/advisory_lock.py`:

```python
"""Session-scoped Postgres advisory lock helper.

Postgres advisory locks scoped to a connection are auto-released when the
connection drops, which is what we want for "single worker runs this loop"
semantics. We use pg_try_advisory_lock (non-blocking) so a second worker
fails fast and exits cleanly rather than queueing.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


@asynccontextmanager
async def pg_try_advisory_lock(
    key: int, *, engine: AsyncEngine
) -> AsyncIterator[bool]:
    """Try to acquire a session-scoped advisory lock. Yields True if
    acquired, False if another session holds it. Lock is released on
    context exit (or connection drop).
    """
    async with engine.connect() as conn:
        got = (await conn.execute(
            sa.text("SELECT pg_try_advisory_lock(:k)"),
            {"k": key},
        )).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                await conn.execute(
                    sa.text("SELECT pg_advisory_unlock(:k)"),
                    {"k": key},
                )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest scripts/tests/test_advisory_lock.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/advisory_lock.py scripts/tests/test_advisory_lock.py
git commit -m "feat(api): add pg_try_advisory_lock async context manager"
```

### Task 0.6: Refactor UW-daily worker guard to use the new helper

**Files:**

- Modify: `src/xenon/api/server.py:335` (the existing UW-daily worker guard)

- [ ] **Step 1: Read the existing pattern**

Read `src/xenon/api/server.py:335` and the surrounding ~30 lines to understand the current UW-daily lock pattern. Note its lock key.

- [ ] **Step 2: Replace with the new helper**

In `src/xenon/api/server.py`, around line 335, replace the inline lock pattern with:

```python
from xenon.api.services.advisory_lock import pg_try_advisory_lock

_UW_DAILY_LOCK_KEY = <existing key value, copied 1:1>

# inside the lifespan / task setup:
async with pg_try_advisory_lock(_UW_DAILY_LOCK_KEY, engine=engine) as got_lock:
    if not got_lock:
        log.info("uw_daily worker already running on another worker; skipping")
        return
    # ... existing UW-daily body unchanged ...
```

Behavior must be byte-identical — only the lock plumbing changes.

- [ ] **Step 3: Run UW-daily tests to confirm parity**

```bash
uv run pytest scripts/tests/ src/xenon/api/tests/ -k uw -xvs
```

Expected: all UW-related tests still pass.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/api/server.py
git commit -m "refactor(uw): UW-daily worker guard uses shared pg_try_advisory_lock helper"
```

### Task 0.7: Web `/api/regime` route — proxy FastAPI

**Files:**

- Modify: `web/app/api/regime/route.ts`
- Test: `web/tests/api-regime-route-proxies.test.ts`

Spec ref: §4.9, §8.0 step 5; current file-read implementation at `web/app/api/regime/route.ts:2, :172`.

- [ ] **Step 1: Write the failing test**

Create `web/tests/api-regime-route-proxies.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";

// The test mocks xenonFetch to return a known payload and asserts that
// the route returns it byte-for-byte without touching the filesystem.
import * as serverFetch from "@/lib/server/xenonFetch";

describe("/api/regime — proxies FastAPI", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the FastAPI payload unchanged", async () => {
    const fakePayload = {
      vcg_tier: "TIER_2",
      cri_tier: "NORMAL",
      binding_tier: "TIER_2",
      binding_side: "vcg",
      vcg_scanned_at: "2026-04-29T15:00:00Z",
      cri_scanned_at: "2026-04-29T15:00:00Z",
      is_stale: false,
      panic_active: false,
    };
    vi.spyOn(serverFetch, "xenonFetch").mockResolvedValue(
      new Response(JSON.stringify(fakePayload), { status: 200 }),
    );

    const { GET } = await import("@/app/api/regime/route");
    const res = await GET(new Request("http://test/api/regime"));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual(fakePayload);
  });

  it("does not read data/cri.json or any other JSON file", async () => {
    const fs = await import("fs/promises");
    const readFileSpy = vi.spyOn(fs, "readFile");

    vi.spyOn(serverFetch, "xenonFetch").mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 }),
    );

    const { GET } = await import("@/app/api/regime/route");
    await GET(new Request("http://test/api/regime"));
    expect(readFileSpy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web && npm test -- api-regime-route-proxies
```

Expected: FAIL — current route reads `data/cri.json`.

- [ ] **Step 3: Replace the route body**

Overwrite `web/app/api/regime/route.ts`:

```typescript
import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/server/xenonFetch";

export const dynamic = "force-dynamic";

export async function GET(_req: Request) {
  const upstream = await xenonFetch("/regime");
  if (!upstream.ok) {
    return NextResponse.json(
      { error: "regime_fetch_failed", upstream_status: upstream.status },
      { status: upstream.status },
    );
  }
  const payload = await upstream.json();
  const res = NextResponse.json(payload);
  res.headers.set("Cache-Control", "private, max-age=30");
  return res;
}
```

- [ ] **Step 4: Run tests**

```bash
cd web && npm test -- api-regime-route-proxies
```

Expected: PASS.

- [ ] **Step 5: Run wider regime-panel test suite to confirm no regression**

```bash
cd web && npm test -- regime
```

Expected: existing `RegimePanel` tests still pass against mocked fetch.

- [ ] **Step 6: Commit**

```bash
git add web/app/api/regime/route.ts web/tests/api-regime-route-proxies.test.ts
git commit -m "feat(web): /api/regime proxies FastAPI; remove data/cri.json file reads"
```

### Task 0.8: `RegimePanel.tsx` — strip client-side CRI recompute

**Files:**

- Modify: `web/components/RegimePanel.tsx` (line ~232)

- [ ] **Step 1: Write the failing test**

Create `web/tests/regime-panel-no-client-recompute.test.tsx`:

```typescript
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { RegimePanel } from "@/components/RegimePanel";

describe("RegimePanel — binding tier comes from server", () => {
  it("renders the server-supplied binding_tier verbatim", async () => {
    const data = {
      vcg_tier: "NORMAL",
      cri_tier: "TIER_2",
      binding_tier: "TIER_2",
      binding_side: "cri",
      vcg_scanned_at: "2026-04-29T15:00:00Z",
      cri_scanned_at: "2026-04-29T15:00:00Z",
      is_stale: false,
      panic_active: false,
    };
    const { findByText } = render(<RegimePanel initialData={data} />);
    // Binding side reflected in the DOM, NOT recomputed on the client.
    expect(await findByText(/CRI/)).toBeInTheDocument();
  });

  it("does not call any client-side CRI scoring helper", async () => {
    // Static check: import the panel module and assert no symbol from
    // the previous client-side recompute helpers is used.
    const mod = await import("@/components/RegimePanel");
    const panelSrc = mod.RegimePanel.toString();
    expect(panelSrc).not.toMatch(/computeCriLive|recomputeCri|liveCriScore/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web && npm test -- regime-panel-no-client-recompute
```

Expected: FAIL — current panel still calls a recompute helper.

- [ ] **Step 3: Strip the recompute branch**

Open `web/components/RegimePanel.tsx`. Around line 232, find the live-CRI-recompute block. Remove the function call and any local state that drove it. Keep display-only overlays (charts, sparklines) but ensure they read from the server payload's `cri_score` / `vcg` fields rather than recomputing.

If a helper file like `web/lib/criLive.ts` exists and is now unused, delete it and any other dead imports. Run `cd web && npm run lint` to surface unused-import errors and fix them.

- [ ] **Step 4: Run tests**

```bash
cd web && npm test -- regime
cd web && npm run lint
cd web && npm run typecheck
```

Expected: tests PASS, lint clean, typecheck clean.

- [ ] **Step 5: Browser smoke test**

```bash
cd web && npm run dev
```

Visit `http://localhost:3000/regime` (or wherever `RegimePanel` mounts). Confirm the panel renders, the binding tier reflects the server payload, and there are no console errors.

- [ ] **Step 6: Commit**

```bash
git add web/components/RegimePanel.tsx web/tests/regime-panel-no-client-recompute.test.tsx
# Add deleted files if any:
git rm web/lib/criLive.ts 2>/dev/null || true
git commit -m "refactor(web): RegimePanel reads binding tier from server, no client-side CRI recompute"
```

### Task 0.9: Phase 0 PR description and verify checklist

**Files:** none (PR description only)

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin <branch-name>
gh pr create --title "VCG/CRI Phase 0 — persistence + plumbing prerequisites" --body "$(cat <<'EOF'
## Summary
- CRI scanner emits boolean `crash_trigger.fired` and `cta.forced_reduction` fields (legacy fields retained).
- New `xenon.scanners.cri.persist()` helper writes `cri_series` rows idempotently per `recorded_date`; wired into `POST /regime/scan`.
- New `pg_try_advisory_lock` helper at `src/xenon/api/services/advisory_lock.py`; UW-daily worker guard refactored to use it.
- Web `/api/regime` rewritten to proxy FastAPI; client-side CRI recompute removed from `RegimePanel.tsx`.
- Phase 0 audit doc at `docs/plans/2026-04-29-vcg-cri-rewiring-audit.md`.

## Test plan
- [ ] `uv run pytest scripts/tests/test_cri_scanner_output_fields.py scripts/tests/test_cri_persist.py scripts/tests/test_advisory_lock.py scripts/tests/test_regime_scan_route_persists.py` — all green.
- [ ] `cd web && npm test -- regime` — all green.
- [ ] Manually `POST /regime/scan` on dev; confirm row in `cri_series`.
- [ ] Open `/regime` page in browser; confirm tier strip renders without console errors and no `data/cri.json` reads in dev logs.
EOF
)"
```

- [ ] **Step 2: Wait for CI green; then merge.**

---

## Phase 1 — PG view + audit table

Spec: §4.2, §4.3, §8.1.

### Task 1.1: Alembic migration — `regime_state` view + `regime_overrides` table

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_regime_state_and_overrides.py`

- [ ] **Step 1: Generate the migration scaffold**

```bash
uv run alembic revision -m "add regime_state view and regime_overrides table"
```

- [ ] **Step 2: Fill in the migration body**

Replace the generated `upgrade()` and `downgrade()` with the following. Source: spec §4.2 (view DDL) and §4.3 (table DDL).

```python
"""add regime_state view and regime_overrides table

Revision ID: <auto>
Revises: <previous head>
Create Date: 2026-04-29 ...
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # 1. regime_state view — thin projection of latest vcg_series + cri_series
    op.execute("""
        CREATE OR REPLACE VIEW regime_state AS
        WITH latest_vcg AS (
            SELECT scanned_at, tier AS vcg_tier_raw, regime AS vcg_regime,
                   ro, edr, bounce, sign_ok, sign_suppressed, pi_panic, vix
            FROM vcg_series ORDER BY scanned_at DESC LIMIT 1
        ),
        latest_cri AS (
            SELECT recorded_at, cri_score, crash_trigger_fired,
                   cta_forced_reduction, vix AS cri_vix
            FROM cri_series ORDER BY recorded_at DESC LIMIT 1
        )
        SELECT
            v.scanned_at      AS vcg_scanned_at,
            v.vcg_tier_raw, v.vcg_regime, v.ro AS vcg_ro,
            v.edr AS vcg_edr, v.bounce AS vcg_bounce,
            v.sign_ok AS vcg_sign_ok, v.pi_panic AS vcg_pi_panic,
            v.vix AS vcg_vix,
            c.recorded_at     AS cri_scanned_at,
            c.cri_score, c.crash_trigger_fired, c.cta_forced_reduction,
            c.cri_vix
        FROM latest_vcg v CROSS JOIN latest_cri c
    """)

    # 2. regime_overrides table — per-scope audit, keyed on submission_id
    op.create_table(
        "regime_overrides",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.Column("submission_id", sa.Text, nullable=False),
        sa.Column("client_attempt_id", sa.Text),
        sa.Column("perm_id", sa.BigInteger),
        sa.Column("ib_order_id", sa.BigInteger),
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("vcg_tier", sa.Text),
        sa.Column("cri_tier", sa.Text),
        sa.Column("binding_side", sa.Text, nullable=False),
        sa.Column("block_reason", sa.Text, nullable=False),
        sa.Column("user_reason", sa.Text, nullable=False),
        sa.Column("order_payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["order_submissions.submission_id"],
            name="fk_regime_overrides_submission",
            deferrable=True, initially="DEFERRED",
        ),
    )
    op.create_index(
        "ix_regime_overrides_ts", "regime_overrides", ["ts"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_regime_overrides_submission",
        "regime_overrides", ["submission_id"],
    )
    op.create_index(
        "ix_regime_overrides_user_ts", "regime_overrides",
        ["user_id", "ts"],
    )
    op.create_index(
        "ix_regime_overrides_scope_ts", "regime_overrides",
        ["account_env", "broker_account", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_regime_overrides_scope_ts", table_name="regime_overrides")
    op.drop_index("ix_regime_overrides_user_ts", table_name="regime_overrides")
    op.drop_index("ix_regime_overrides_submission", table_name="regime_overrides")
    op.drop_index("ix_regime_overrides_ts", table_name="regime_overrides")
    op.drop_table("regime_overrides")
    op.execute("DROP VIEW IF EXISTS regime_state")
```

- [ ] **Step 3: Run the migration**

```bash
uv run alembic upgrade head
```

Expected: applies cleanly. Run `uv run alembic downgrade -1` then `upgrade head` to confirm the downgrade also works.

- [ ] **Step 4: Add SQLAlchemy reflection in `src/xenon/db/schema.py`**

Append (after the existing tables):

```python
regime_overrides = Table(
    "regime_overrides",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ts", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("user_id", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("submission_id", Text, nullable=False),
    Column("client_attempt_id", Text),
    Column("perm_id", BigInteger),
    Column("ib_order_id", BigInteger),
    Column("route", Text, nullable=False),
    Column("vcg_tier", Text),
    Column("cri_tier", Text),
    Column("binding_side", Text, nullable=False),
    Column("block_reason", Text, nullable=False),
    Column("user_reason", Text, nullable=False),
    Column("order_payload", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["submission_id"], ["order_submissions.submission_id"],
        name="fk_regime_overrides_submission",
        deferrable=True, initially="DEFERRED",
    ),
)
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/migrations/versions/*_add_regime_state_and_overrides.py src/xenon/db/schema.py
git commit -m "feat(db): add regime_state view and regime_overrides table"
```

### Task 1.2: Integration tests — view shape + deferred FK

**Files:**

- Create: `scripts/tests/test_regime_state_view.py`
- Create: `scripts/tests/test_regime_overrides_audit.py`

- [ ] **Step 1: Write the view-shape test**

Create `scripts/tests/test_regime_state_view.py`:

```python
"""regime_state view returns the latest row of each scanner; zero rows
when either underlying table is empty."""
import datetime as dt
import sqlalchemy as sa

from xenon.db.schema import vcg_series, cri_series


def test_view_zero_rows_when_either_empty(test_engine):
    with test_engine.begin() as c:
        c.execute(sa.delete(vcg_series))
        c.execute(sa.delete(cri_series))
        rows = c.execute(sa.text("SELECT * FROM regime_state")).all()
        assert rows == []


def test_view_returns_latest_of_each(test_engine):
    now = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)
    with test_engine.begin() as c:
        c.execute(sa.delete(vcg_series))
        c.execute(sa.delete(cri_series))

        c.execute(sa.insert(vcg_series).values(
            scanned_at=now,
            payload={"signal": {"vcg": 2.7, "tier": 2, "regime": "ACTIVE",
                                "ro": 1, "edr": 0, "bounce": 0,
                                "sign_ok": True, "sign_suppressed": False,
                                "pi_panic": 0.0, "vix": 29.0,
                                "attribution": {}}},
        ))
        c.execute(sa.insert(cri_series).values(
            recorded_at=now,
            cri_level=42.0,
            payload={"date": "2026-04-29", "vix": 29.0, "vvix": 110.0,
                     "spy": 510.0, "vix_5d_roc": 1.0, "vvix_vix_ratio": 3.8,
                     "spx_100d_ma": 505.0, "spx_distance_pct": 1.0,
                     "cor1m": 0.45, "cor1m_previous_close": 0.46,
                     "cor1m_5d_change": -0.02, "realized_vol": 18.0,
                     "cri": {"score": 42.0, "components": {}},
                     "cta": {"exposure_pct": 70.0, "forced_reduction": False,
                             "forced_reduction_pct": 0.0, "selling_usd_b": 0.0},
                     "menthorq_cta": {"score": 0.0},
                     "crash_trigger": {"triggered": False, "fired": False}},
        ))

        rows = c.execute(sa.text("""
            SELECT vcg_tier_raw, vcg_regime, cri_score, crash_trigger_fired
            FROM regime_state
        """)).all()
        assert len(rows) == 1
        assert rows[0].vcg_tier_raw == 2
        assert rows[0].vcg_regime == "ACTIVE"
        assert float(rows[0].cri_score) == 42.0
        assert rows[0].crash_trigger_fired is False
```

- [ ] **Step 2: Write the deferred-FK / audit insert test**

Create `scripts/tests/test_regime_overrides_audit.py`:

```python
"""regime_overrides FK is DEFERRABLE INITIALLY DEFERRED — insert with a
non-existent submission_id fails at COMMIT, not at INSERT."""
import sqlalchemy as sa
import pytest

from xenon.db.schema import regime_overrides


def test_orphan_submission_id_fails_at_commit(test_engine):
    with pytest.raises(sa.exc.IntegrityError):
        with test_engine.begin() as c:
            c.execute(sa.insert(regime_overrides).values(
                user_id="u1", account_env="paper", broker="ib",
                broker_account="DU123", submission_id="SUB-DOES-NOT-EXIST",
                route="POST /orders/place", binding_side="cri",
                block_reason="test", user_reason="test test test",
                order_payload={},
            ))
            # Within the transaction the INSERT succeeds because FK is DEFERRED.
            # The IntegrityError fires when the with-block commits.


def test_audit_insert_with_existing_submission_succeeds(test_engine, fixture_submission_id):
    """fixture_submission_id is provided by conftest — reserves a row in
    order_submissions and yields its id."""
    with test_engine.begin() as c:
        c.execute(sa.insert(regime_overrides).values(
            user_id="u1", account_env="paper", broker="ib",
            broker_account="DU123", submission_id=fixture_submission_id,
            route="POST /orders/place", binding_side="cri",
            block_reason="CRI CRITICAL — non-hedge entries blocked",
            user_reason="contrarian play, sized small",
            order_payload={"symbol": "AAPL", "quantity": 1},
        ))
    # Read it back
    with test_engine.connect() as c:
        row = c.execute(sa.select(regime_overrides).where(
            regime_overrides.c.submission_id == fixture_submission_id
        )).one()
        assert row.binding_side == "cri"
```

If `fixture_submission_id` does not exist in the project's `conftest.py`, add it to `scripts/tests/conftest.py`:

```python
import secrets
import pytest
import sqlalchemy as sa


@pytest.fixture
def fixture_submission_id(test_engine):
    """Reserve a row in order_submissions and yield its submission_id.
    Cleans up after the test."""
    from xenon.db.schema import order_submissions  # adjust import to actual table

    sub_id = f"SUB-TEST-{secrets.token_hex(6)}"
    with test_engine.begin() as c:
        c.execute(sa.insert(order_submissions).values(
            submission_id=sub_id, user_id="u1",
            account_env="paper", broker="ib", broker_account="DU123",
            payload={},
            # ...other required cols per schema.py
        ))
    yield sub_id
    with test_engine.begin() as c:
        c.execute(sa.delete(order_submissions).where(
            order_submissions.c.submission_id == sub_id
        ))
```

- [ ] **Step 3: Run the tests**

```bash
uv run pytest scripts/tests/test_regime_state_view.py scripts/tests/test_regime_overrides_audit.py -xvs
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/test_regime_state_view.py scripts/tests/test_regime_overrides_audit.py scripts/tests/conftest.py
git commit -m "test(db): regime_state view shape + regime_overrides deferred FK"
```

### Task 1.3: Phase 1 PR

- [ ] **Step 1: Push branch and open PR**

```bash
gh pr create --title "VCG/CRI Phase 1 — regime_state view + regime_overrides table" --body "$(cat <<'EOF'
## Summary
- New Alembic migration: `regime_state` view (thin projection of latest vcg_series + cri_series); `regime_overrides` table keyed on submission_id with deferred FK to order_submissions.
- SQLAlchemy reflection for `regime_overrides` in schema.py.
- Integration tests for view shape and deferred FK behaviour.

## Test plan
- [ ] `uv run alembic upgrade head` clean on dev DB.
- [ ] `uv run alembic downgrade -1 && uv run alembic upgrade head` round-trip clean.
- [ ] `uv run pytest scripts/tests/test_regime_state_view.py scripts/tests/test_regime_overrides_audit.py` — all green.
EOF
)"
```

---

## Phase 2 — Classifier + dependency + `/regime` endpoint

Spec: §4.4, §4.7, §8.2.

### Task 2.1: `RegimeState` dataclass + classifier (no DB)

**Files:**

- Create: `src/xenon/api/services/regime_state.py`
- Test: `scripts/tests/test_regime_state_classifier.py`

- [ ] **Step 1: Write the failing classifier table tests**

Create `scripts/tests/test_regime_state_classifier.py`:

```python
"""Classifier unit tests — pure functions, no DB."""
import datetime as dt
import pytest

from xenon.api.services.regime_state import (
    classify, RegimeState, TierLabel,
)


_NOW = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)
_FRESH = _NOW - dt.timedelta(minutes=10)
_STALE = _NOW - dt.timedelta(hours=2)


def _row(**kw):
    """Minimal regime_state row dict for classify()."""
    base = dict(
        vcg_scanned_at=_FRESH, vcg_tier_raw=None, vcg_regime="DIVERGENCE",
        vcg_ro=0, vcg_edr=0, vcg_bounce=0, vcg_sign_ok=True,
        vcg_pi_panic=0.0, vcg_vix=20.0,
        cri_scanned_at=_FRESH, cri_score=20.0,
        crash_trigger_fired=False, cta_forced_reduction=False, cri_vix=20.0,
    )
    base.update(kw)
    return base


@pytest.mark.parametrize("row,expected", [
    # NORMAL on both sides
    (_row(), ("NORMAL", "NORMAL", "NORMAL", "none")),
    # VCG EDR
    (_row(vcg_edr=1, vcg_regime="WATCH"), ("EDR", "NORMAL", "EDR", "vcg")),
    # VCG TIER_2
    (_row(vcg_tier_raw=2, vcg_regime="ACTIVE"), ("TIER_2", "NORMAL", "TIER_2", "vcg")),
    # VCG TIER_1
    (_row(vcg_tier_raw=1, vcg_regime="ACTIVE"), ("TIER_1", "NORMAL", "TIER_1", "vcg")),
    # PANIC via vcg_pi_panic >= 1.0
    (_row(vcg_pi_panic=1.0, vcg_vix=49.0), ("PANIC", "NORMAL", "PANIC", "vcg")),
    # CRI HIGH
    (_row(cri_score=60.0), ("NORMAL", "TIER_2", "TIER_2", "cri")),
    # CRI CRITICAL via score
    (_row(cri_score=80.0), ("NORMAL", "TIER_1", "TIER_1", "cri")),
    # CRI CRITICAL via crash_trigger_fired
    (_row(crash_trigger_fired=True), ("NORMAL", "TIER_1", "TIER_1", "cri")),
    # Both binding — strictest wins, side="both" if equal
    (_row(vcg_tier_raw=2, cri_score=60.0), ("TIER_2", "TIER_2", "TIER_2", "both")),
    # VCG TIER_1, CRI TIER_2 — VCG binds
    (_row(vcg_tier_raw=1, cri_score=60.0), ("TIER_1", "TIER_2", "TIER_1", "vcg")),
    # Stale VCG only
    (_row(vcg_scanned_at=_STALE), ("UNKNOWN", "NORMAL", "EDR", "vcg")),
    # Stale both
    (_row(vcg_scanned_at=_STALE, cri_scanned_at=_STALE), ("UNKNOWN", "UNKNOWN", "EDR", "both")),
])
def test_classifier_table(row, expected):
    state = classify(row, now=_NOW, max_age_s=90 * 60)
    got = (state.vcg_tier, state.cri_tier, state.binding_tier, state.binding_side)
    assert got == expected
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_regime_state_classifier.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'classify'`.

- [ ] **Step 3: Implement `classify`**

Create `src/xenon/api/services/regime_state.py`:

```python
"""Regime state classifier and FastAPI dependency.

Reads from the `regime_state` view (Phase 1 migration) and projects raw
scanner outputs into the canonical six-tier ladder used by RegimeGate.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal, Optional

import sqlalchemy as sa


TierLabel = Literal["NORMAL", "EDR", "TIER_2", "TIER_1", "PANIC", "UNKNOWN"]

# Ordinal ranking for binding_tier. UNKNOWN is pegged to EDR (throttle-not-block).
_TIER_ORDINAL: dict[TierLabel, int] = {
    "NORMAL": 0, "EDR": 1, "UNKNOWN": 1, "TIER_2": 2, "TIER_1": 3, "PANIC": 4,
}


@dataclass(frozen=True)
class RegimeState:
    vcg_tier: TierLabel
    cri_tier: TierLabel
    binding_tier: TierLabel
    binding_side: Literal["vcg", "cri", "both", "none"]
    vcg_scanned_at: Optional[dt.datetime]
    cri_scanned_at: Optional[dt.datetime]
    is_stale: bool
    panic_active: bool
    raw: dict = field(default_factory=dict)


def _classify_vcg(row, *, now, max_age_s) -> TierLabel:
    if row["vcg_scanned_at"] is None:
        return "UNKNOWN"
    if (now - row["vcg_scanned_at"]).total_seconds() > max_age_s:
        return "UNKNOWN"
    if (row.get("vcg_pi_panic") or 0) >= 1.0:
        return "PANIC"
    tier = row.get("vcg_tier_raw")
    if tier == 1:
        return "TIER_1"
    if tier == 2:
        return "TIER_2"
    if (row.get("vcg_edr") or 0) == 1:
        return "EDR"
    return "NORMAL"


def _classify_cri(row, *, now, max_age_s) -> TierLabel:
    if row["cri_scanned_at"] is None:
        return "UNKNOWN"
    if (now - row["cri_scanned_at"]).total_seconds() > max_age_s:
        return "UNKNOWN"
    if row.get("crash_trigger_fired") or (row.get("cri_score") or 0) >= 75:
        return "TIER_1"
    if (row.get("cri_score") or 0) >= 50:
        return "TIER_2"
    return "NORMAL"


def _binding(vcg_t: TierLabel, cri_t: TierLabel) -> tuple[TierLabel, str]:
    v_ord, c_ord = _TIER_ORDINAL[vcg_t], _TIER_ORDINAL[cri_t]
    if v_ord == 0 and c_ord == 0:
        return "NORMAL", "none"
    if v_ord > c_ord:
        return vcg_t, "vcg"
    if c_ord > v_ord:
        return cri_t, "cri"
    # equal ordinal: pick the more specific label, attribute to "both"
    chosen = vcg_t if vcg_t != "UNKNOWN" else cri_t
    return chosen if chosen != "UNKNOWN" else "EDR", "both"


def classify(row: dict, *, now: dt.datetime, max_age_s: int) -> RegimeState:
    vcg_t = _classify_vcg(row, now=now, max_age_s=max_age_s)
    cri_t = _classify_cri(row, now=now, max_age_s=max_age_s)
    binding_tier, binding_side = _binding(vcg_t, cri_t)
    is_stale = "UNKNOWN" in (vcg_t, cri_t)
    panic_active = (
        (row.get("vcg_vix") or 0) >= 48 or (row.get("cri_vix") or 0) >= 48
    )
    return RegimeState(
        vcg_tier=vcg_t, cri_tier=cri_t,
        binding_tier=binding_tier, binding_side=binding_side,
        vcg_scanned_at=row["vcg_scanned_at"],
        cri_scanned_at=row["cri_scanned_at"],
        is_stale=is_stale, panic_active=panic_active,
        raw=dict(row),
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_regime_state_classifier.py -xvs
```

Expected: PASS — all 13 parametrized cases.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/regime_state.py scripts/tests/test_regime_state_classifier.py
git commit -m "feat(api): RegimeState classifier — pure-function tier mapping"
```

### Task 2.2: `get_regime_state` FastAPI dependency with TTL cache

**Files:**

- Modify: `src/xenon/api/services/regime_state.py`
- Test: `scripts/tests/test_get_regime_state_cache.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_get_regime_state_cache.py`:

```python
"""get_regime_state caches per (account_env, broker_account) for 30 s.
Setting XENON_REGIME_CACHE_TTL_S=0 opts out of caching for tests."""
import asyncio
import datetime as dt
import os

import pytest
import sqlalchemy as sa

from xenon.api.services.regime_state import get_regime_state, _cache_clear
from xenon.db.schema import vcg_series, cri_series


@pytest.fixture(autouse=True)
def clear_cache():
    _cache_clear()
    yield
    _cache_clear()


@pytest.mark.asyncio
async def test_first_call_reads_db_second_uses_cache(monkeypatch, test_engine, fake_account_scope):
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "30")

    # Seed
    now = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)
    with test_engine.begin() as c:
        c.execute(sa.delete(vcg_series))
        c.execute(sa.delete(cri_series))
        c.execute(sa.insert(cri_series).values(
            recorded_at=now, cri_level=20.0,
            payload={"date": "2026-04-29", "cri": {"score": 20.0, "components": {}},
                     "cta": {}, "menthorq_cta": {}, "crash_trigger": {"fired": False, "triggered": False},
                     "vix": 20.0, "vvix": 90.0, "spy": 510.0,
                     "vix_5d_roc": 0, "vvix_vix_ratio": 4.5, "spx_100d_ma": 505.0,
                     "spx_distance_pct": 1.0, "cor1m": 0.4, "cor1m_previous_close": 0.4,
                     "cor1m_5d_change": 0, "realized_vol": 18.0},
        ))

    state_a = await get_regime_state(scope=fake_account_scope)
    state_b = await get_regime_state(scope=fake_account_scope)
    assert state_a is state_b, "cached read must return same dataclass instance"


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache(monkeypatch, test_engine, fake_account_scope):
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "0")
    state_a = await get_regime_state(scope=fake_account_scope)
    state_b = await get_regime_state(scope=fake_account_scope)
    assert state_a is not state_b
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_get_regime_state_cache.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'get_regime_state'`.

- [ ] **Step 3: Implement the dep + cache**

Append to `src/xenon/api/services/regime_state.py`:

```python
import os
import time

from fastapi import Depends
import sqlalchemy as sa

from xenon.execution.account_scope import AccountScope, get_account_scope
from xenon.db.engine import engine  # adjust to actual project import


_cache: dict[tuple[str, str], tuple[float, RegimeState]] = {}


def _cache_clear() -> None:
    _cache.clear()


def _cache_get(key, ttl_s) -> Optional[RegimeState]:
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, state = entry
    if ttl_s == 0 or (time.monotonic() - cached_at) > ttl_s:
        return None
    return state


def _cache_set(key, state) -> None:
    _cache[key] = (time.monotonic(), state)


async def _read_regime_row() -> dict:
    """Single-row SELECT against the regime_state view."""
    async with engine.connect() as conn:
        row = (await conn.execute(sa.text("SELECT * FROM regime_state"))).mappings().first()
        return dict(row) if row is not None else {
            "vcg_scanned_at": None, "cri_scanned_at": None,
        }


async def get_regime_state(
    scope: AccountScope = Depends(get_account_scope),
) -> RegimeState:
    """FastAPI dep — returns the current RegimeState, cached per scope."""
    ttl_s = int(os.environ.get("XENON_REGIME_CACHE_TTL_S", "30"))
    max_age_s = int(os.environ.get("XENON_REGIME_MAX_AGE_S", str(90 * 60)))
    key = (scope.account_env, scope.broker_account)

    cached = _cache_get(key, ttl_s)
    if cached is not None:
        return cached

    row = await _read_regime_row()
    state = classify(row, now=dt.datetime.now(dt.timezone.utc), max_age_s=max_age_s)
    if ttl_s > 0:
        _cache_set(key, state)
    return state
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_get_regime_state_cache.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/regime_state.py scripts/tests/test_get_regime_state_cache.py
git commit -m "feat(api): get_regime_state Depends with 30s in-process TTL cache"
```

### Task 2.3: `GET /regime` and `GET /regime/overrides` endpoints

**Files:**

- Create: `src/xenon/api/routes/regime.py`
- Modify: `src/xenon/api/server.py` (register the new router)
- Test: `src/xenon/api/tests/test_regime_routes.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/api/tests/test_regime_routes.py`:

```python
"""GET /regime returns the current RegimeState as JSON."""
from fastapi.testclient import TestClient


def test_get_regime_returns_payload(client: TestClient):
    res = client.get("/regime")
    assert res.status_code == 200
    body = res.json()
    for k in ("vcg_tier", "cri_tier", "binding_tier", "binding_side",
              "vcg_scanned_at", "cri_scanned_at", "is_stale", "panic_active"):
        assert k in body


def test_get_regime_overrides_paginates(client: TestClient):
    res = client.get("/regime/overrides?limit=10")
    assert res.status_code == 200
    body = res.json()
    assert "items" in body and isinstance(body["items"], list)
    assert "limit" in body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest src/xenon/api/tests/test_regime_routes.py -xvs
```

Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement the router**

Create `src/xenon/api/routes/regime.py`:

```python
"""GET /regime + GET /regime/overrides."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Response
import sqlalchemy as sa

from xenon.api.services.regime_state import RegimeState, get_regime_state
from xenon.execution.account_scope import AccountScope, get_account_scope
from xenon.db.engine import engine
from xenon.db.schema import regime_overrides


router = APIRouter()


@router.get("/regime")
async def get_regime(
    response: Response,
    state: RegimeState = Depends(get_regime_state),
):
    response.headers["Cache-Control"] = "private, max-age=30"
    payload = asdict(state)
    payload.pop("raw", None)  # hide internal raw dict
    return payload


@router.get("/regime/overrides")
async def list_regime_overrides(
    limit: int = Query(50, ge=1, le=200),
    scope: AccountScope = Depends(get_account_scope),
):
    async with engine.connect() as conn:
        rows = (await conn.execute(
            sa.select(regime_overrides)
            .where(
                regime_overrides.c.account_env == scope.account_env,
                regime_overrides.c.broker_account == scope.broker_account,
            )
            .order_by(regime_overrides.c.ts.desc())
            .limit(limit)
        )).mappings().all()
    return {"items": [dict(r) for r in rows], "limit": limit}
```

- [ ] **Step 4: Register the router in `server.py`**

In `src/xenon/api/server.py`, after the `app = FastAPI(...)` line:

```python
from xenon.api.routes.regime import router as regime_router
app.include_router(regime_router)
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest src/xenon/api/tests/test_regime_routes.py -xvs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/routes/regime.py src/xenon/api/server.py src/xenon/api/tests/test_regime_routes.py
git commit -m "feat(api): GET /regime and GET /regime/overrides endpoints"
```

### Task 2.4: `RegimePanel.tsx` per-scanner tier strip

**Files:**

- Modify: `web/components/RegimePanel.tsx`

- [ ] **Step 1: Write the failing component test**

Create `web/tests/regime-panel-tier-strip.test.tsx`:

```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { RegimePanel } from "@/components/RegimePanel";

describe("RegimePanel — per-scanner tier strip", () => {
  it("renders both VCG-R and CRI tiers with binding-side highlight", () => {
    render(<RegimePanel initialData={{
      vcg_tier: "TIER_2", cri_tier: "NORMAL",
      binding_tier: "TIER_2", binding_side: "vcg",
      vcg_scanned_at: "2026-04-29T15:00:00Z",
      cri_scanned_at: "2026-04-29T15:00:00Z",
      is_stale: false, panic_active: false,
    }} />);

    expect(screen.getByTestId("regime-tier-vcg")).toHaveTextContent("TIER_2");
    expect(screen.getByTestId("regime-tier-cri")).toHaveTextContent("NORMAL");
    expect(screen.getByTestId("regime-tier-vcg")).toHaveAttribute("data-binding", "true");
    expect(screen.getByTestId("regime-tier-cri")).toHaveAttribute("data-binding", "false");
  });

  it("renders stale-data banner when is_stale", () => {
    render(<RegimePanel initialData={{
      vcg_tier: "UNKNOWN", cri_tier: "NORMAL",
      binding_tier: "EDR", binding_side: "both",
      vcg_scanned_at: "2026-04-29T13:00:00Z",
      cri_scanned_at: "2026-04-29T15:00:00Z",
      is_stale: true, panic_active: false,
    }} />);
    expect(screen.getByText(/regime data stale/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web && npm test -- regime-panel-tier-strip
```

Expected: FAIL.

- [ ] **Step 3: Implement the strip**

Edit `web/components/RegimePanel.tsx`. Add the tier strip block (use existing styles; mimic structure of any existing similar strip in the codebase if present). Minimal example:

```tsx
function RegimeTierStrip({ data }: { data: RegimeData }) {
  return (
    <div className="regime-tier-strip" role="status">
      <span
        data-testid="regime-tier-vcg"
        data-binding={
          data.binding_side === "vcg" || data.binding_side === "both"
        }
        className={tierClass(data.vcg_tier, data.binding_side === "vcg")}
      >
        VCG-R: {data.vcg_tier}
      </span>
      <span
        data-testid="regime-tier-cri"
        data-binding={
          data.binding_side === "cri" || data.binding_side === "both"
        }
        className={tierClass(data.cri_tier, data.binding_side === "cri")}
      >
        CRI: {data.cri_tier}
      </span>
      {data.is_stale && (
        <span className="regime-stale-banner">
          regime data stale (&gt;90 min) — sized conservatively
        </span>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests + lint + typecheck**

```bash
cd web && npm test -- regime
cd web && npm run lint
cd web && npm run typecheck
```

Expected: green.

- [ ] **Step 5: Browser smoke test**

```bash
cd web && npm run dev
```

Visit `/regime`. Confirm both badges render; freshness ages display; stale banner appears when data is old.

- [ ] **Step 6: Commit**

```bash
git add web/components/RegimePanel.tsx web/tests/regime-panel-tier-strip.test.tsx
git commit -m "feat(web): RegimePanel renders per-scanner tier strip with binding highlight"
```

### Task 2.5: Phase 2 PR

- [ ] Open PR with the standard verify checklist (classifier table tests; cache hit/miss; `/regime` endpoint smoke; tier strip renders correctly; no order-path effects yet).

---

## Phase 3 — RegimeGate + order-route integration + CI guards

Spec: §4.5, §4.6, §7.5, §8.3.

### Task 3.1: `GateResult` dataclass + `_max_loss_usd` helper

**Files:**

- Create: `src/xenon/api/services/regime_gate.py`
- Test: `scripts/tests/test_max_loss_usd.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_max_loss_usd.py`:

```python
"""_max_loss_usd computes worst-case USD loss for defined-risk structures."""
import math
import pytest

from xenon.api.services.regime_gate import _max_loss_usd


def _order(legs, contracts=1, structure="long_single"):
    """Build a minimal PreflightRequest-shaped dict for the helper."""
    return {"legs": legs, "contracts": contracts, "structure": structure}


def test_long_single_call():
    order = _order(legs=[{"side": "long", "type": "call", "limit_price": 2.50}],
                   structure="long_call")
    assert _max_loss_usd(order) == 2.50 * 100  # premium × 100


def test_long_single_call_multiple_contracts():
    order = _order(legs=[{"side": "long", "type": "call", "limit_price": 2.50}],
                   contracts=5, structure="long_call")
    assert _max_loss_usd(order) == 2.50 * 100 * 5


def test_debit_vertical():
    order = _order(legs=[
        {"side": "long",  "type": "call", "strike": 100, "limit_price": 3.0},
        {"side": "short", "type": "call", "strike": 105, "limit_price": 1.0},
    ], structure="long_call_vertical")
    # net debit 2.0; width 5; max loss = net debit × 100 = 200
    assert _max_loss_usd(order) == 200.0


def test_credit_vertical():
    order = _order(legs=[
        {"side": "short", "type": "put", "strike": 100, "limit_price": 3.0},
        {"side": "long",  "type": "put", "strike":  95, "limit_price": 1.5},
    ], structure="short_put_vertical")
    # width 5, net credit 1.5 → max loss = (5 - 1.5) * 100 = 350
    assert _max_loss_usd(order) == 350.0


def test_unknown_structure_returns_inf():
    order = _order(legs=[], structure="naked_call")
    assert _max_loss_usd(order) == math.inf
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_max_loss_usd.py -xvs
```

Expected: FAIL.

- [ ] **Step 3: Implement `_max_loss_usd`**

Create `src/xenon/api/services/regime_gate.py`:

```python
"""RegimeGate: order-path veto driven by RegimeState."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GateDecision(str, Enum):
    OK = "ok"
    THROTTLE = "throttle"
    BLOCK = "block"


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str
    bind: str  # "vcg" | "cri" | "both" | "none"
    max_loss_cap_usd: Optional[float] = None
    cover_ratio: Optional[float] = None


_DEFINED_RISK_STRUCTURES = {
    # Long-only and debit verticals + defined-risk credit structures.
    "long_call", "long_put",
    "long_call_vertical", "long_put_vertical",   # debit verticals
    "short_call_vertical", "short_put_vertical", # credit verticals
    "iron_condor", "iron_butterfly", "long_butterfly",
}


def _net_debit_or_credit(legs: list[dict]) -> float:
    """Positive = net debit (premium paid); negative = net credit."""
    total = 0.0
    for leg in legs:
        sign = +1 if leg["side"] == "long" else -1
        total += sign * float(leg["limit_price"])
    return total


def _vertical_width(legs: list[dict]) -> float:
    strikes = sorted({float(leg["strike"]) for leg in legs})
    return strikes[-1] - strikes[0] if len(strikes) >= 2 else 0.0


def _max_loss_usd(order: dict) -> float:
    """Worst-case USD loss for a defined-risk structure.

    Returns math.inf for naked or otherwise unbounded-loss structures so
    the gate's THROTTLE cap rejects them by construction.
    """
    structure = order.get("structure")
    contracts = int(order.get("contracts", 1))
    legs = order.get("legs", [])
    if structure not in _DEFINED_RISK_STRUCTURES:
        return math.inf

    net = _net_debit_or_credit(legs)

    if structure in {"long_call", "long_put"}:
        return net * 100 * contracts  # net is positive (debit)

    if structure in {"long_call_vertical", "long_put_vertical"}:
        return net * 100 * contracts  # net debit × 100 × contracts

    if structure in {"short_call_vertical", "short_put_vertical"}:
        width = _vertical_width(legs)
        net_credit = -net  # net is negative for a credit spread
        return (width - net_credit) * 100 * contracts

    if structure in {"iron_condor", "iron_butterfly", "long_butterfly"}:
        # Use the existing project helper if present; otherwise compute
        # per-wing width and take max wing loss. Falls back to inf.
        # TODO in this task only: prefer to wire to an existing helper if
        # docs/trading/options-structures.json carries pricing fields.
        return math.inf

    return math.inf
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_max_loss_usd.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/regime_gate.py scripts/tests/test_max_loss_usd.py
git commit -m "feat(api): _max_loss_usd helper for defined-risk structures"
```

### Task 3.2: `_is_hedge` predicate

**Files:**

- Modify: `src/xenon/api/services/regime_gate.py`
- Test: `scripts/tests/test_is_hedge.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_is_hedge.py`:

```python
"""_is_hedge identifies defined-risk hedges on the canonical hedge underlyings."""
import pytest

from xenon.api.services.regime_gate import _is_hedge


HEDGE_UNDERLYINGS = {"HYG", "JNK", "LQD", "SPY", "SPX", "VIX"}


def _order(symbol, structure, leg_types=("put",), sides=("long",)):
    legs = [{"symbol": symbol, "side": s, "type": t, "limit_price": 1.0,
             "strike": 100} for t, s in zip(leg_types, sides)]
    return {"symbol": symbol, "structure": structure, "legs": legs, "contracts": 1}


@pytest.mark.parametrize("symbol", sorted(HEDGE_UNDERLYINGS))
def test_long_put_on_hedge_underlying_is_hedge(symbol):
    if symbol == "VIX":
        # VIX hedges are long calls / call-spreads, not puts
        return
    assert _is_hedge(_order(symbol, "long_put"))


def test_long_put_on_aapl_is_not_hedge():
    assert not _is_hedge(_order("AAPL", "long_put"))


def test_long_put_vertical_on_spy_is_hedge():
    assert _is_hedge({
        "symbol": "SPY", "structure": "long_put_vertical",
        "legs": [
            {"symbol": "SPY", "side": "long",  "type": "put", "strike": 500, "limit_price": 5.0},
            {"symbol": "SPY", "side": "short", "type": "put", "strike": 490, "limit_price": 2.0},
        ],
        "contracts": 1,
    })


def test_long_call_on_vix_is_hedge():
    assert _is_hedge(_order("VIX", "long_call", leg_types=("call",), sides=("long",)))


def test_naked_short_call_on_hyg_is_not_hedge():
    assert not _is_hedge({
        "symbol": "HYG", "structure": "naked_call",
        "legs": [{"symbol": "HYG", "side": "short", "type": "call",
                  "strike": 78, "limit_price": 0.50}],
        "contracts": 1,
    })
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_is_hedge.py -xvs
```

Expected: FAIL.

- [ ] **Step 3: Implement `_is_hedge`**

Append to `src/xenon/api/services/regime_gate.py`:

```python
_CREDIT_HEDGE_UNDERLYINGS = {"HYG", "JNK", "LQD"}
_EQUITY_INDEX_HEDGE_UNDERLYINGS = {"SPY", "SPX"}
_VOL_HEDGE_UNDERLYINGS = {"VIX"}

_PUT_HEDGE_STRUCTURES = {"long_put", "long_put_vertical"}
_CALL_HEDGE_STRUCTURES = {"long_call", "long_call_vertical"}


def _is_hedge(order: dict) -> bool:
    """True iff order matches a canonical hedge structure on a canonical
    hedge underlying. Phase 1 audit (docs/plans/2026-04-29-vcg-cri-rewiring-audit.md
    §3) is the source of truth for the structure set."""
    sym = (order.get("symbol") or "").upper()
    structure = order.get("structure")
    if not sym or not structure:
        return False

    if sym in (_CREDIT_HEDGE_UNDERLYINGS | _EQUITY_INDEX_HEDGE_UNDERLYINGS):
        return structure in _PUT_HEDGE_STRUCTURES
    if sym in _VOL_HEDGE_UNDERLYINGS:
        return structure in _CALL_HEDGE_STRUCTURES
    return False
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_is_hedge.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/regime_gate.py scripts/tests/test_is_hedge.py
git commit -m "feat(api): _is_hedge predicate for canonical hedge structures"
```

### Task 3.3: `RegimeGate.veto` decision tree

**Files:**

- Modify: `src/xenon/api/services/regime_gate.py`
- Test: `scripts/tests/test_regime_gate.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_regime_gate.py`:

```python
"""RegimeGate.veto decision tree — table tests."""
import datetime as dt
import math
import pytest

from xenon.api.services.regime_gate import RegimeGate, GateDecision
from xenon.api.services.regime_state import RegimeState


_NOW = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)


def _state(binding, side="vcg"):
    return RegimeState(
        vcg_tier=binding if side in ("vcg", "both") else "NORMAL",
        cri_tier=binding if side in ("cri", "both") else "NORMAL",
        binding_tier=binding,
        binding_side=side,
        vcg_scanned_at=_NOW, cri_scanned_at=_NOW,
        is_stale=False, panic_active=binding == "PANIC",
    )


def _non_hedge_order():
    return {"symbol": "AAPL", "structure": "long_call",
            "legs": [{"symbol": "AAPL", "side": "long", "type": "call",
                      "strike": 200, "limit_price": 5.0}],
            "contracts": 1}


def _hedge_order():
    return {"symbol": "HYG", "structure": "long_put",
            "legs": [{"symbol": "HYG", "side": "long", "type": "put",
                      "strike": 78, "limit_price": 1.0}],
            "contracts": 1}


@pytest.mark.parametrize("binding,is_hedge,expected", [
    ("NORMAL", False, GateDecision.OK),
    ("NORMAL", True,  GateDecision.OK),
    ("EDR",    False, GateDecision.THROTTLE),
    ("EDR",    True,  GateDecision.THROTTLE),
    ("TIER_2", False, GateDecision.THROTTLE),
    ("TIER_2", True,  GateDecision.THROTTLE),
    ("TIER_1", False, GateDecision.BLOCK),
    ("TIER_1", True,  GateDecision.OK),    # hedges always pass on TIER_1+
    ("PANIC",  False, GateDecision.BLOCK),
    ("PANIC",  True,  GateDecision.OK),
    ("UNKNOWN", False, GateDecision.THROTTLE),
])
def test_veto_table(binding, is_hedge, expected):
    state = _state(binding)
    order = _hedge_order() if is_hedge else _non_hedge_order()
    result = RegimeGate.veto(order, state, bankroll_usd=100_000)
    assert result.decision == expected


def test_throttle_strict_on_tier2_returns_125_cover_ratio():
    state = _state("TIER_2")
    res = RegimeGate.veto(_non_hedge_order(), state, bankroll_usd=100_000)
    assert res.decision == GateDecision.THROTTLE
    assert res.cover_ratio == 1.25
    assert res.max_loss_cap_usd == 0.0125 * 100_000


def test_throttle_soft_on_edr_keeps_cover_ratio_at_one():
    state = _state("EDR")
    res = RegimeGate.veto(_non_hedge_order(), state, bankroll_usd=100_000)
    assert res.cover_ratio == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_regime_gate.py -xvs
```

Expected: FAIL.

- [ ] **Step 3: Implement `RegimeGate.veto`**

Append to `src/xenon/api/services/regime_gate.py`:

```python
class RegimeGate:
    """Stateless veto helper. Returns GateResult; the caller (order route)
    is responsible for translating it into HTTP responses (422 / 409)."""

    @staticmethod
    def veto(order: dict, state, bankroll_usd: float) -> GateResult:
        binding = state.binding_tier
        bind = state.binding_side
        cap = 0.0125 * bankroll_usd  # half of 2.5% Four Gates default

        # Tier 1 / PANIC: BLOCK non-hedges
        if binding in ("TIER_1", "PANIC"):
            if _is_hedge(order):
                return GateResult(
                    decision=GateDecision.OK, reason="", bind=bind,
                )
            return GateResult(
                decision=GateDecision.BLOCK,
                reason=f"{binding} — non-hedge entries blocked",
                bind=bind,
            )

        # TIER_2: strict throttle (cover-ratio bumped to 1.25)
        if binding == "TIER_2":
            return GateResult(
                decision=GateDecision.THROTTLE,
                reason="TIER_2 — risk-budget halved; cover-ratio tightened",
                bind=bind,
                max_loss_cap_usd=cap,
                cover_ratio=1.25,
            )

        # EDR / UNKNOWN: soft throttle
        if binding in ("EDR", "UNKNOWN"):
            return GateResult(
                decision=GateDecision.THROTTLE,
                reason=f"{binding} — risk-budget halved",
                bind=bind,
                max_loss_cap_usd=cap,
                cover_ratio=1.0,
            )

        return GateResult(
            decision=GateDecision.OK, reason="", bind=bind,
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_regime_gate.py scripts/tests/test_max_loss_usd.py scripts/tests/test_is_hedge.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/regime_gate.py scripts/tests/test_regime_gate.py
git commit -m "feat(api): RegimeGate.veto decision tree with cap and cover-ratio"
```

### Task 3.4: Parameterize covered-call cover-ratio in `guards.py`

**Files:**

- Modify: `src/xenon/api/guards.py`
- Test: `scripts/tests/test_covered_call_cover_ratio_param.py`

- [ ] **Step 1: Inspect the current predicate**

Read `src/xenon/api/guards.py` and locate the covered-call predicate (search for `1.0`, `100 *`, `short_call`, `cover`). Confirm it is currently hard-coded to a 1.0 ratio.

- [ ] **Step 2: Write the failing test**

Create `scripts/tests/test_covered_call_cover_ratio_param.py`:

```python
"""The covered-call guard accepts a cover-ratio argument (default 1.0)
so RegimeGate's TIER_2 throttle can pass 1.25."""
import pytest

from xenon.api.guards import covered_call_satisfied  # adjust to actual symbol


def _order_with_short_call(short_contracts, long_shares):
    return {
        "structure": "covered_call",
        "contracts": short_contracts,
        "long_shares_held": long_shares,
        # ...other minimum-viable fields
    }


def test_default_ratio_is_1_0():
    # 1 short call needs 100 long shares
    assert covered_call_satisfied(_order_with_short_call(1, 100))
    assert not covered_call_satisfied(_order_with_short_call(1, 99))


def test_tighter_ratio_125_requires_more_cover():
    # 1 short call at 1.25 needs 125 long shares
    assert covered_call_satisfied(_order_with_short_call(1, 125), cover_ratio=1.25)
    assert not covered_call_satisfied(_order_with_short_call(1, 124), cover_ratio=1.25)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest scripts/tests/test_covered_call_cover_ratio_param.py -xvs
```

Expected: FAIL — `cover_ratio` kwarg not accepted.

- [ ] **Step 4: Parameterize**

In `src/xenon/api/guards.py`, change the function signature to accept `cover_ratio: float = 1.0` and use it in the comparison:

```python
def covered_call_satisfied(order: dict, *, cover_ratio: float = 1.0) -> bool:
    short_contracts = order.get("contracts", 0)
    long_shares = order.get("long_shares_held", 0)
    required = cover_ratio * short_contracts * 100
    return long_shares >= required
```

(Adjust to the actual function name + signature in `guards.py`. Keep the original signature working so all existing call sites continue to pass.)

- [ ] **Step 5: Run tests + the wider guards test suite**

```bash
uv run pytest scripts/tests/test_covered_call_cover_ratio_param.py -xvs
uv run pytest scripts/tests/ src/xenon/api/tests/ -k guard -xvs
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/guards.py scripts/tests/test_covered_call_cover_ratio_param.py
git commit -m "refactor(guards): parameterize covered-call cover_ratio (default 1.0)"
```

### Task 3.5: Wire `RegimeGate.veto` into `POST /orders/place`

**Files:**

- Modify: `src/xenon/api/server.py:1901` (`POST /orders/place` handler)
- Test: `web/tests/order-place-regime-block.test.ts`, `order-place-regime-throttle.test.ts`

- [ ] **Step 1: Write the failing FastAPI-harness tests**

Create `web/tests/order-place-regime-block.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { fastapiHarness } from "./fastapiHarness";

describe("POST /orders/place — TIER_1 BLOCK", () => {
  it("returns 409 with structured body for non-hedge entries", async () => {
    await fastapiHarness.setRegimeState({
      vcg_tier: "TIER_1",
      cri_tier: "NORMAL",
      binding_tier: "TIER_1",
      binding_side: "vcg",
    });
    const res = await fastapiHarness.post("/orders/place", {
      symbol: "AAPL",
      structure: "long_call",
      legs: [
        {
          symbol: "AAPL",
          side: "long",
          type: "call",
          strike: 200,
          limit_price: 5.0,
        },
      ],
      contracts: 1,
    });
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body.decision).toBe("block");
    expect(body.binding_side).toBe("vcg");
  });
});
```

Create `web/tests/order-place-regime-throttle.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { fastapiHarness } from "./fastapiHarness";

describe("POST /orders/place — TIER_2 THROTTLE", () => {
  it("returns 422 resize_required when max_loss_usd exceeds cap", async () => {
    await fastapiHarness.setRegimeState({
      vcg_tier: "TIER_2",
      cri_tier: "NORMAL",
      binding_tier: "TIER_2",
      binding_side: "vcg",
    });
    // Bankroll 100k → cap = 1250. Order with max_loss = 5000 must trip.
    const res = await fastapiHarness.post("/orders/place", {
      symbol: "AAPL",
      structure: "long_call",
      legs: [
        {
          symbol: "AAPL",
          side: "long",
          type: "call",
          strike: 200,
          limit_price: 50.0,
        },
      ],
      contracts: 10, // 50 * 100 * 10 = 50000 max loss
    });
    expect(res.status).toBe(422);
    const body = await res.json();
    expect(body.decision).toBe("resize_required");
    expect(body.max_loss_cap_usd).toBeCloseTo(1250);
  });
});
```

- [ ] **Step 2: Add the `setRegimeState` helper to `fastapiHarness.ts`**

In `web/tests/fastapiHarness.ts`, add a method that POSTs to a test-mode-only seeding endpoint, or pre-seeds `app.state.regime` directly (mirrors the existing pre-seeding pattern from the conftest mentioned in CLAUDE.md memory `feedback_testclient_skips_lifespan`).

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd web && npm test -- order-place-regime
```

Expected: FAIL — gate not yet wired.

- [ ] **Step 4: Wire the gate into `POST /orders/place`**

In `src/xenon/api/server.py`, around line 1901, modify the `POST /orders/place` handler:

```python
from fastapi import HTTPException
from xenon.api.services.regime_state import RegimeState, get_regime_state
from xenon.api.services.regime_gate import RegimeGate, GateDecision, _max_loss_usd
from xenon.api.guards import covered_call_satisfied

@app.post("/orders/place")
async def post_orders_place(
    body: PlaceOrderRequest,  # existing model
    request: Request,
    state: RegimeState = Depends(get_regime_state),
    scope: AccountScope = Depends(get_account_scope),
):
    # Build the dict the gate consumes (existing internal shape, may need
    # a small adapter):
    order = body.model_dump()
    bankroll = float(scope.net_liq_usd or 0.0)

    gate = RegimeGate.veto(order, state, bankroll_usd=bankroll)

    if gate.decision == GateDecision.BLOCK:
        if not request.query_params.get("override") == "true":
            raise HTTPException(status_code=409, detail={
                "decision": "block",
                "reason": gate.reason,
                "binding_side": gate.bind,
                "vcg_tier": state.vcg_tier,
                "cri_tier": state.cri_tier,
            })
        # Override path — see Task 3.7 for audit insertion.
        await _write_regime_override_row(
            order=order, gate=gate, state=state, scope=scope,
            user_reason=body.override_reason or "",
        )

    if gate.decision == GateDecision.THROTTLE:
        ml = _max_loss_usd(order)
        if ml > (gate.max_loss_cap_usd or 0):
            raise HTTPException(status_code=422, detail={
                "decision": "resize_required",
                "max_loss_cap_usd": gate.max_loss_cap_usd,
                "binding_tier": state.binding_tier,
                "reason": gate.reason,
            })
        # Plumb the tightened cover-ratio into the existing covered-call guard:
        if not covered_call_satisfied(order, cover_ratio=gate.cover_ratio or 1.0):
            raise HTTPException(status_code=422, detail={
                "decision": "cover_ratio",
                "cover_ratio": gate.cover_ratio,
                "binding_tier": state.binding_tier,
            })

    # ... existing place order logic unchanged ...
```

- [ ] **Step 5: Run tests**

```bash
cd web && npm test -- order-place-regime
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py web/tests/order-place-regime-block.test.ts web/tests/order-place-regime-throttle.test.ts web/tests/fastapiHarness.ts
git commit -m "feat(api): wire RegimeGate.veto into POST /orders/place (BLOCK + THROTTLE)"
```

### Task 3.6: Wire override path with audit row

**Files:**

- Modify: `src/xenon/api/server.py` (helper `_write_regime_override_row`)
- Test: `web/tests/order-place-regime-override.test.ts`

- [ ] **Step 1: Write the failing override test**

Create `web/tests/order-place-regime-override.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { fastapiHarness } from "./fastapiHarness";

describe("POST /orders/place?override=true — audit row", () => {
  it("blocks without override; succeeds with override + valid reason; writes audit row", async () => {
    await fastapiHarness.setRegimeState({
      vcg_tier: "NORMAL",
      cri_tier: "TIER_1",
      binding_tier: "TIER_1",
      binding_side: "cri",
    });
    const order = {
      symbol: "AAPL",
      structure: "long_call",
      legs: [
        {
          symbol: "AAPL",
          side: "long",
          type: "call",
          strike: 200,
          limit_price: 5.0,
        },
      ],
      contracts: 1,
    };

    const blocked = await fastapiHarness.post("/orders/place", order);
    expect(blocked.status).toBe(409);

    const overridden = await fastapiHarness.post(
      "/orders/place?override=true",
      {
        ...order,
        override_reason: "earnings catalyst already priced in, contrarian",
      },
    );
    expect(overridden.status).toBe(200);

    const overrides = await fastapiHarness.getJson("/regime/overrides?limit=5");
    expect(overrides.items.length).toBeGreaterThan(0);
    expect(overrides.items[0].user_reason).toMatch(/earnings catalyst/);
  });

  it("rejects override with empty reason (HTTP 400)", async () => {
    const res = await fastapiHarness.post("/orders/place?override=true", {
      symbol: "AAPL",
      structure: "long_call",
      legs: [
        {
          symbol: "AAPL",
          side: "long",
          type: "call",
          strike: 200,
          limit_price: 5.0,
        },
      ],
      contracts: 1,
      override_reason: "",
    });
    expect(res.status).toBe(400);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web && npm test -- order-place-regime-override
```

Expected: FAIL.

- [ ] **Step 3: Implement `_write_regime_override_row`**

Add to `src/xenon/api/server.py`:

```python
import sqlalchemy as sa
from xenon.db.schema import regime_overrides
from xenon.execution.orders_store import reserve_submission_id  # adjust import


async def _write_regime_override_row(
    *, order: dict, gate, state, scope, user_reason: str,
) -> None:
    """Reserve a submission_id and write the audit row in the same
    transaction. Caller continues with broker submission afterwards."""
    if not user_reason or len(user_reason.strip()) < 10:
        raise HTTPException(status_code=400, detail={
            "error": "override_reason_required",
            "min_chars": 10,
        })

    # Reserve submission_id within the same async transaction the order
    # path would use anyway (existing helper in orders_store).
    async with engine.begin() as conn:
        sub_id = await reserve_submission_id(conn, scope, order)
        await conn.execute(sa.insert(regime_overrides).values(
            user_id=scope.user_id,
            account_env=scope.account_env,
            broker=scope.broker,
            broker_account=scope.broker_account,
            submission_id=sub_id,
            client_attempt_id=order.get("client_attempt_id"),
            route="POST /orders/place",
            vcg_tier=state.vcg_tier,
            cri_tier=state.cri_tier,
            binding_side=gate.bind,
            block_reason=gate.reason,
            user_reason=user_reason.strip(),
            order_payload=order,
        ))
    # The post-broker UPDATE that fills perm_id / ib_order_id lives in the
    # existing orders_store flow; add a sibling helper that updates
    # regime_overrides where submission_id matches (Task 3.7).
```

- [ ] **Step 4: Run tests**

```bash
cd web && npm test -- order-place-regime-override
```

Expected: PASS (the audit row test). Reason-required test should pass without further changes.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py web/tests/order-place-regime-override.test.ts
git commit -m "feat(api): override path writes regime_overrides audit row + min-reason validation"
```

### Task 3.7: Post-fill UPDATE: `perm_id` and `ib_order_id` linkage

**Files:**

- Modify: `src/xenon/execution/orders_store.py` (or sibling helper file)
- Test: `scripts/tests/test_regime_overrides_post_fill.py`

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_regime_overrides_post_fill.py`:

```python
"""After IB returns, perm_id + ib_order_id are filled into the audit row
for the matching submission_id."""
import sqlalchemy as sa
from xenon.db.schema import regime_overrides
from xenon.execution.orders_store import mark_regime_override_perm_id


def test_post_fill_update(test_engine, fixture_submission_id):
    with test_engine.begin() as c:
        c.execute(sa.insert(regime_overrides).values(
            user_id="u1", account_env="paper", broker="ib", broker_account="DU1",
            submission_id=fixture_submission_id,
            route="POST /orders/place", binding_side="cri",
            block_reason="x", user_reason="long enough reason here",
            order_payload={},
        ))
    mark_regime_override_perm_id(
        submission_id=fixture_submission_id,
        perm_id=12345, ib_order_id=67,
    )
    with test_engine.connect() as c:
        row = c.execute(sa.select(regime_overrides).where(
            regime_overrides.c.submission_id == fixture_submission_id
        )).one()
        assert row.perm_id == 12345
        assert row.ib_order_id == 67
```

- [ ] **Step 2: Run + fail**

```bash
uv run pytest scripts/tests/test_regime_overrides_post_fill.py -xvs
```

Expected: FAIL — `mark_regime_override_perm_id` not defined.

- [ ] **Step 3: Implement**

In `src/xenon/execution/orders_store.py`:

```python
import sqlalchemy as sa
from xenon.db.schema import regime_overrides

def mark_regime_override_perm_id(*, submission_id: str, perm_id: int, ib_order_id: int) -> None:
    """Sibling to the existing `mark_ib_order_id` helper. Idempotent —
    runs unconditionally in the post-broker flow even when no audit row
    exists (override-less orders)."""
    with engine.begin() as conn:
        conn.execute(
            sa.update(regime_overrides)
            .where(regime_overrides.c.submission_id == submission_id)
            .values(perm_id=perm_id, ib_order_id=ib_order_id)
        )
```

Then call `mark_regime_override_perm_id(...)` immediately after the existing `mark_ib_order_id(...)` call (search for it in `orders_store.py` around line 324 per the spec).

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_regime_overrides_post_fill.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/orders_store.py scripts/tests/test_regime_overrides_post_fill.py
git commit -m "feat(orders): post-fill UPDATE on regime_overrides with perm_id and ib_order_id"
```

### Task 3.8: `POST /orders/modify` — gating with delta-order rules

**Files:**

- Modify: `src/xenon/api/server.py:2270`
- Test: `web/tests/order-modify-regime-gating.test.ts`

Spec ref: §4.6.1.

- [ ] **Step 1: Failing test**

Create `web/tests/order-modify-regime-gating.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { fastapiHarness } from "./fastapiHarness";

describe("POST /orders/modify — gated only on new exposure", () => {
  it("pure price modify is allowed under TIER_1", async () => {
    await fastapiHarness.setRegimeState({
      binding_tier: "TIER_1",
      binding_side: "cri",
      vcg_tier: "NORMAL",
      cri_tier: "TIER_1",
    });
    // Modify price only, same quantity, same side.
    const res = await fastapiHarness.post("/orders/modify", {
      submission_id: "SUB-EXISTING",
      new_price: 5.5,
      new_quantity: 1, // assume original was qty 1
    });
    expect(res.status).not.toBe(409);
  });

  it("quantity-decrease modify is allowed under TIER_1", async () => {
    const res = await fastapiHarness.post("/orders/modify", {
      submission_id: "SUB-EXISTING",
      new_price: 5.0,
      new_quantity: 0, // closing
    });
    expect(res.status).not.toBe(409);
  });

  it("quantity-increase modify gates the delta", async () => {
    await fastapiHarness.setRegimeState({
      binding_tier: "TIER_1",
      binding_side: "cri",
      vcg_tier: "NORMAL",
      cri_tier: "TIER_1",
    });
    const res = await fastapiHarness.post("/orders/modify", {
      submission_id: "SUB-EXISTING",
      new_price: 5.0,
      new_quantity: 5, // assume original was 1
    });
    // Delta order is non-hedge → BLOCK
    expect(res.status).toBe(409);
  });
});
```

- [ ] **Step 2: Run + fail**

```bash
cd web && npm test -- order-modify-regime
```

Expected: FAIL.

- [ ] **Step 3: Implement modify gating**

In `src/xenon/api/server.py:2270`, before the existing modify body:

```python
@app.post("/orders/modify", dependencies=[Depends(require_mode_verified)])
async def post_orders_modify(
    body: ModifyOrderRequest, request: Request,
    state: RegimeState = Depends(get_regime_state),
    scope: AccountScope = Depends(get_account_scope),
):
    # Load original order from order_submissions (existing helper).
    original = await load_submission(body.submission_id)

    same_side = body.new_side in (None, original.side)
    is_qty_decrease = (body.new_quantity is not None
                       and body.new_quantity < original.quantity)
    is_qty_increase = (body.new_quantity is not None
                       and body.new_quantity > original.quantity)
    is_price_only = (body.new_price is not None
                     and body.new_quantity in (None, original.quantity)
                     and same_side)

    if is_price_only or is_qty_decrease:
        # Skip gate — pure-price or downsize.
        pass
    elif is_qty_increase:
        # Build a synthetic "delta" order representing the increment;
        # gate that.
        delta_qty = body.new_quantity - original.quantity
        delta_order = {**original.payload, "contracts": delta_qty}
        gate = RegimeGate.veto(delta_order, state,
                               bankroll_usd=float(scope.net_liq_usd or 0))
        _apply_gate_or_raise(gate, delta_order, scope, state, request, body)
    else:
        # Side change / structure change — gate the entire new order.
        new_order = {**original.payload, **body.changes()}
        gate = RegimeGate.veto(new_order, state,
                               bankroll_usd=float(scope.net_liq_usd or 0))
        _apply_gate_or_raise(gate, new_order, scope, state, request, body)

    # ... existing modify body unchanged ...
```

`_apply_gate_or_raise` is a small helper that mirrors the BLOCK / THROTTLE branches from Task 3.5 (extract them now into a shared helper if not already).

- [ ] **Step 4: Run tests**

```bash
cd web && npm test -- order-modify-regime
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py web/tests/order-modify-regime-gating.test.ts
git commit -m "feat(api): modify gating — pure-price/decrease bypass; quantity-increase gates delta"
```

### Task 3.9: Wire wizard combo-submit through the gate

**Files:**

- Modify: `src/xenon/execution/combo_wizard/session.py:327` (`_orders_place_from_body` caller)
- Test: `scripts/tests/test_wizard_submit_through_gate.py`

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_wizard_submit_through_gate.py`:

```python
"""Wizard combo-submit calls the gate so it cannot bypass the order-route
allowlist (in-process route bypass vector — see CLAUDE.md memory)."""
import pytest
from xenon.execution.combo_wizard.session import submit_combo


@pytest.mark.asyncio
async def test_wizard_combo_submit_invokes_gate(monkeypatch):
    called = {}

    async def _fake_veto(order, state, bankroll_usd):
        called["yes"] = True
        from xenon.api.services.regime_gate import GateResult, GateDecision
        return GateResult(decision=GateDecision.OK, reason="", bind="none")

    monkeypatch.setattr(
        "xenon.api.services.regime_gate.RegimeGate.veto", _fake_veto
    )

    await submit_combo(_minimal_combo_payload(), scope=_fake_scope(), state=_fake_state())
    assert called.get("yes")


def _minimal_combo_payload():
    return {"symbol": "SPY", "structure": "long_put_vertical", ...}
def _fake_scope(): ...
def _fake_state(): ...
```

(Fill in fake helpers per the actual session.py interface.)

- [ ] **Step 2: Run + fail**

```bash
uv run pytest scripts/tests/test_wizard_submit_through_gate.py -xvs
```

Expected: FAIL.

- [ ] **Step 3: Wire the gate**

In `src/xenon/execution/combo_wizard/session.py:327`, where `_orders_place_from_body` is called, add a `RegimeGate.veto` call upstream and apply the same BLOCK/THROTTLE/override semantics. If `_orders_place_from_body` already runs the gate inside its own body (after Task 3.5), confirm by reading; if so, the wizard call inherits the gate for free and this task only adds a defensive assertion + integration test.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest scripts/tests/test_wizard_submit_through_gate.py -xvs
git add src/xenon/execution/combo_wizard/session.py scripts/tests/test_wizard_submit_through_gate.py
git commit -m "feat(wizard): combo-submit runs through RegimeGate (close in-process bypass)"
```

### Task 3.10: Wizard 422 / 409 client handling + override modal

**Files:**

- Modify: `web/components/order-wizard/*` (specific files from Phase 0 audit)
- Test: `web/tests/wizard-regime-modals.test.tsx`

- [ ] **Step 1: Failing test**

Create `web/tests/wizard-regime-modals.test.tsx` covering:

- 409 BLOCK → modal renders with reason; textarea `<10 chars` disables Confirm; ≥10 chars enables and POSTs `?override=true`.
- 422 resize_required → "Trim to fit?" prompt pre-fills max contract count whose `max_loss_usd ≤ cap`; Apply resubmits without override.

(Full test code follows the Vitest + Testing-Library patterns already in `web/tests/`.)

- [ ] **Step 2: Run + fail**, **Step 3: Implement**, **Step 4: Run + pass**.

- [ ] **Step 5: Browser smoke test**

```bash
cd web && npm run dev
```

Manually trip a TIER_1 BLOCK and a TIER_2 throttle on the wizard; confirm both modals render correctly and submit through.

- [ ] **Step 6: Commit**

```bash
git add web/components/order-wizard/* web/tests/wizard-regime-modals.test.tsx
git commit -m "feat(wizard): 409 BLOCK override modal + 422 resize_required Trim-to-fit prompt"
```

### Task 3.11: CI guard — `order_path_regime_gate_called.py`

**Files:**

- Create: `scripts/checks/order_path_regime_gate_called.py`
- Modify: `.github/workflows/ci.yml::order-path-guards`

- [ ] **Step 1: Implement the static check**

Create `scripts/checks/order_path_regime_gate_called.py`:

```python
#!/usr/bin/env python3
"""CI guard: every order entry-point must call RegimeGate.veto.

Walks the order-path allowlist (same source as order_path_caller_allowlist.py)
and asserts the function body of each contains a lexical `RegimeGate.veto(`
or an explicit allowlist exemption (e.g. cancel/refresh routes).
"""
from __future__ import annotations
import ast
import pathlib
import sys


_ENTRY_POINTS = [
    ("src/xenon/api/server.py", "post_orders_place"),
    ("src/xenon/api/server.py", "post_orders_modify"),
    ("src/xenon/execution/combo_wizard/session.py", "submit_combo"),
]

_EXEMPT = {
    ("src/xenon/api/server.py", "post_orders_cancel"),
    ("src/xenon/api/server.py", "post_orders_refresh"),
}


def _function_body_text(path: str, fn_name: str) -> str:
    src = pathlib.Path(path).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == fn_name:
            return ast.get_source_segment(src, node) or ""
    return ""


def main() -> int:
    failures = []
    for path, fn in _ENTRY_POINTS:
        body = _function_body_text(path, fn)
        if not body:
            failures.append(f"{path}::{fn} not found")
            continue
        if "RegimeGate.veto(" not in body:
            failures.append(
                f"{path}::{fn} does not call RegimeGate.veto. "
                "If this is intentional, add to _EXEMPT in this guard "
                "and document why."
            )
    if failures:
        print("Order-path regime-gate guard FAILED:", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1
    print("order_path_regime_gate_called: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Wire into CI**

In `.github/workflows/ci.yml`, find the `order-path-guards` job and add a step:

```yaml
- name: order_path_regime_gate_called
  run: uv run python scripts/checks/order_path_regime_gate_called.py
```

- [ ] **Step 3: Run locally**

```bash
uv run python scripts/checks/order_path_regime_gate_called.py
```

Expected: `order_path_regime_gate_called: OK`.

- [ ] **Step 4: Negative-case verification**

Temporarily comment out `RegimeGate.veto(...)` in `post_orders_place`. Re-run the guard. Confirm it FAILS with a useful message. Restore the call.

- [ ] **Step 5: Commit**

```bash
git add scripts/checks/order_path_regime_gate_called.py .github/workflows/ci.yml
git commit -m "ci: order_path_regime_gate_called — static check enforces RegimeGate.veto on entry points"
```

### Task 3.12: CI guard — `/api/regime` no file reads

**Files:**

- Modify: `scripts/checks/no_json_fallback_on_order_path.py` (or sibling)

- [ ] **Step 1: Extend the existing check**

Add `web/app/api/regime/route.ts` to the file list the guard scans, and assert:

- No `readDataFile`, `readFile` from `fs`/`fs/promises`.
- No `JSON.parse(fs.readFileSync(...))`.
- No reference to `data/cri.json` or `data/cri_scheduled`.

- [ ] **Step 2: Run + commit**

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
git add scripts/checks/no_json_fallback_on_order_path.py
git commit -m "ci: extend file-read guard to web/app/api/regime/route.ts"
```

### Task 3.13: Phase 3 PR (Playwright golden path + close out)

**Files:**

- Create: `web/e2e/regime-gate-flow.spec.ts`

- [ ] **Step 1: Playwright spec**

```typescript
import { test, expect } from "@playwright/test";

test("regime gate — BLOCK → modal → override → blotter tag", async ({
  page,
}) => {
  await page.goto("/wizard");
  // Test fixture: seed PG to TIER_1 state via a test-only endpoint.
  await page.request.post("/__test__/regime/seed", {
    data: {
      binding_tier: "TIER_1",
      binding_side: "cri",
      vcg_tier: "NORMAL",
      cri_tier: "TIER_1",
    },
  });

  // Build a non-hedge order in the wizard and submit
  // (selectors per actual wizard component).
  await page.locator('[data-testid="symbol-input"]').fill("AAPL");
  // ... build long-call legs ...
  await page.locator('[data-testid="wizard-submit"]').click();

  // BLOCK modal appears
  await expect(
    page.locator('[data-testid="regime-block-modal"]'),
  ).toBeVisible();
  await page
    .locator('[data-testid="override-reason"]')
    .fill("earnings catalyst already priced in, contrarian");
  await page.locator('[data-testid="override-confirm"]').click();

  // Order goes through; blotter shows Overridden tag
  await page.goto("/blotter");
  await expect(
    page.locator('[data-testid="row-tag-overridden"]').first(),
  ).toBeVisible();
});
```

- [ ] **Step 2: Run + commit + open PR**

```bash
cd web && npx playwright test regime-gate-flow.spec.ts
git add web/e2e/regime-gate-flow.spec.ts
git commit -m "test(e2e): regime gate golden path — block, override, blotter tag"

gh pr create --title "VCG/CRI Phase 3 — RegimeGate + order-route integration + CI guards" --body "..."
```

---

## Phase 4 — Scheduler + outbox emit

Spec: §4.1, §4.8, §8.4.

### Task 4.1: `_vcg_cri_scan_loop` in lifespan

**Files:**

- Modify: `src/xenon/api/server.py` (lifespan around line 238)

- [ ] **Step 1: Failing integration test**

Create `scripts/tests/test_vcg_cri_scan_loop.py`:

```python
"""_vcg_cri_scan_loop runs scans, persists rows, and emits transition
events to the outbox when tiers change."""
import asyncio
import pytest
import sqlalchemy as sa

from xenon.db.schema import vcg_series, cri_series, events_outbox
from xenon.api.server import _vcg_cri_scan_loop, _read_latest_regime_tiers


@pytest.mark.asyncio
async def test_loop_emits_outbox_on_tier_transition(test_engine, monkeypatch):
    # Seed prior state at NORMAL/NORMAL.
    with test_engine.begin() as c:
        c.execute(sa.delete(vcg_series))
        c.execute(sa.delete(cri_series))
        c.execute(sa.delete(events_outbox))
        # Seed a NORMAL row so last_seen baseline is established.
        # ...
    # Patch the CLI runners to inject TIER_2 on next tick.
    monkeypatch.setattr("xenon.api.server._run_vcg_scan_and_persist", _stub_seed_tier2)
    monkeypatch.setattr("xenon.api.server._run_cri_scan_and_persist", _stub_noop)

    # Run one tick of the loop under a fast cadence
    monkeypatch.setattr("xenon.api.server._SCAN_INTERVAL_S", 0)
    asyncio.create_task(_vcg_cri_scan_loop())
    await asyncio.sleep(0.5)  # let one tick run

    # Assert outbox row appeared
    with test_engine.connect() as c:
        rows = c.execute(sa.select(events_outbox).where(
            events_outbox.c.kind == "regime_transition"
        )).all()
        assert len(rows) >= 1
```

- [ ] **Step 2: Run + fail**, **Step 3: Implement** (per spec §4.1 code listing — copy in directly), **Step 4: Run + pass**, **Step 5: Commit**.

### Task 4.2: UNKNOWN suppression test

- [ ] One additional test asserting that a transition involving `UNKNOWN` does NOT emit. Implement by setting up stale fixture rows.

### Task 4.3: Update CLAUDE.md startup checklist

- [ ] Patch `CLAUDE.md` § Startup Checklist to mention the consolidated VCG/CRI loop. Commit.

### Task 4.4: Phase 4 PR.

---

## Phase 5 — Docs + closeout

### Task 5.1: Update CLAUDE.md Order-Path Guards

- [ ] Add the regime-gate guard to the table in `CLAUDE.md` § Order-Path Guards (Layers 1+2). Mention the new check and its purpose. Commit.

### Task 5.2: Close out backlog item §7

- [ ] Update `docs/todo-backlog.md` § 7 to mark this work shipped, link to this design doc and plan. Move any deferred follow-ups (remove `data/cri.json` archive once no readers; auto-staged hedge orders Phase 2) to the Inbox.

### Task 5.3: Final regression sweep

- [ ] Run the full Python test suite: `uv run pytest`.
- [ ] Run the full web suite: `cd web && npm test && npm run lint && npm run typecheck && npx playwright test`.
- [ ] Open a closeout PR titled "VCG/CRI Phase 5 — docs and backlog closeout" with a checklist linking each prior phase's PR.

---

## Self-review

### Spec coverage

- §1 / §1.1 errata + §2 in/out scope → Phase 0 + plan Goal/Architecture.
- §3 architecture diagram → Tasks 1.1, 2.1–2.3, 3.1–3.13, 4.1.
- §3.1 invariants → distributed across tasks; CI guards in Task 3.11/3.12 enforce invariants 1, 3, 5 mechanically.
- §3.2 throttle table → Tasks 3.3 (table tests) + 3.5/3.6 (HTTP behavior).
- §3.2.1 throttle contract → Tasks 3.3 + 3.5 (cap-and-resize) + 3.4 (cover-ratio).
- §4.1 scheduler → Task 4.1.
- §4.2 view → Task 1.1.
- §4.3 audit table → Task 1.1 (DDL) + Task 1.2 (FK test) + Task 3.6 (insert) + Task 3.7 (post-fill UPDATE).
- §4.4 classifier → Task 2.1 + 2.2.
- §4.5 / §4.5.1 gate → Tasks 3.1 + 3.2 + 3.3.
- §4.6 / §4.6.1 / §4.6.2 modify rules + override protocol → Tasks 3.5, 3.6, 3.8.
- §4.7 endpoints → Task 2.3.
- §4.8 outbox emit → Task 4.1 + 4.2.
- §4.9 web → Tasks 0.7, 0.8, 2.4, 3.10.
- §5 walkthroughs → exercised by tests in Tasks 3.5, 3.6, 4.1.
- §6 errors → covered by Tasks 1.2 (FK), 3.6 (override audit failure → rollback), 4.1 (advisory lock + UNKNOWN suppression).
- §7 testing → distributed across all tasks (each task includes its tests).
- §7.5 CI guards → Tasks 3.11 + 3.12, landing in Phase 3 per spec.
- §8 phases → matches plan phases 0–5.

**No spec gaps identified.**

### Placeholder scan

- One residual `TODO` in Task 3.1 step 3 (iron condor / butterfly max-loss helper). Marked as falling through to `inf` so the THROTTLE cap still rejects them safely; flagged for the Phase 1 audit to confirm whether `options-structures.json` carries the pricing fields needed to compute exact max loss for those structures. Acceptable for v1.
- All other "TODO" / "TBD" strings appear only inside spec-quoted prose, not as plan-level placeholders.

### Type / signature consistency

- `RegimeState` (dataclass) — defined in Task 2.1, imported and used identically in Tasks 2.2, 2.3, 3.3, 3.5, 3.6, 3.8, 4.1.
- `GateResult` fields (`decision`, `reason`, `bind`, `max_loss_cap_usd`, `cover_ratio`) — defined in Task 3.1, used identically in Tasks 3.3, 3.5, 3.6.
- `_max_loss_usd` signature `(order: dict) -> float` consistent across Tasks 3.1 and 3.5.
- `mark_regime_override_perm_id(*, submission_id, perm_id, ib_order_id)` — defined in Task 3.7, called from `orders_store` post-fill flow in same task.
- `pg_try_advisory_lock(key, *, engine)` async context manager — defined in Task 0.5, used in Tasks 0.6 and 4.1.
- `_is_hedge(order: dict) -> bool` consistent in Tasks 3.2, 3.3.

No signature drift detected.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-29-vcg-cri-strategies-rewiring.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration with two-stage review.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
