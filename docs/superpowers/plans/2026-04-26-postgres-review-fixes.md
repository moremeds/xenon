# Postgres Migration Review Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all verified issues from the tribunal review (Codex + Claude) and ultrareview of PR #52.

**Architecture:** 9 tasks grouped by blast radius — migration data-loss bugs first, then engine consolidation, then schema/constraint hardening, then test infrastructure. Each task is an independent commit.

**Tech Stack:** SQLAlchemy Core, Alembic, psycopg, pytest

---

## Source: Combined Findings

| ID  | Source               | Severity  | Summary                                                          |
| --- | -------------------- | --------- | ---------------------------------------------------------------- |
| T1  | Codex P2 + Claude    | CRITICAL  | Migration script drops all UW flow events (dict treated as list) |
| T2  | Codex P2 + Claude    | CRITICAL  | Migration script drops all UW stats history (wrong key + type)   |
| T3  | Codex P1             | IMPORTANT | Migration script `import duckdb` fails after dep removal         |
| T4  | Claude + Ultrareview | IMPORTANT | Migration script not idempotent for 7 tables                     |
| T5  | Codex + Claude       | IMPORTANT | 5 private sync engines bypass centralized `get_sync_engine()`    |
| T6  | Claude               | IMPORTANT | `dispose_engine()` leaks the sync engine                         |
| T7  | Codex + Claude       | IMPORTANT | URL dialect conversion fragile, duplicated 5 places              |
| T8  | Claude               | IMPORTANT | api/tests conftest sets DATABASE_URL to sync dialect             |
| T9  | Claude               | IMPORTANT | Test truncation lists miss 8+ tables                             |
| T10 | Ultrareview #2       | HIGH      | `register_from_snapshot()` select-then-insert race               |
| T11 | Ultrareview #3       | HIGH      | `wizard_protection` missing UNIQUE(session_id)                   |
| T12 | Ultrareview #1       | HIGH      | Outbox trigger can abort business writes on bad channel          |
| T13 | Ultrareview #4       | MEDIUM    | Order idempotency constraint nullable columns                    |
| T14 | Ultrareview #5       | MEDIUM    | `flow_event_key` unique but nullable, insert path skips it       |
| T15 | Ultrareview #6       | MEDIUM    | Missing indexes on child tables                                  |
| T16 | Ultrareview #7       | MEDIUM    | UW stats upsert is last-writer-wins                              |
| T17 | Ultrareview #8       | MEDIUM    | UW stats load selects entire table                               |
| T18 | Ultrareview #9       | MEDIUM    | Portfolio snapshot delete-insert without concurrency guard       |
| T19 | Claude               | IMPORTANT | `apply_modify_by_perm_id` TOCTOU on separate connections         |
| T20 | Claude               | MINOR     | Unused imports `json`, `uuid` in `combo_wizard.py`               |
| T21 | Ultrareview #10      | LOW       | Sequence not renamed after table rename migration                |

## Deferred (no patch needed now)

- **T16 (UW stats last-writer-wins):** Only one process writes stats. Risk is theoretical until multi-worker deploy. Document the constraint; revisit if architecture changes.
- **T21 (sequence name mismatch):** Cosmetic. Functional. Not worth a migration that renames a sequence. Document in a comment.

---

### Task 1: Fix migration script data-loss bugs (T1, T2, T3, T4)

**Files:**

- Modify: `scripts/migrations/migrate_to_postgres.py:16` (duckdb guard)
- Modify: `scripts/migrations/migrate_to_postgres.py:447-477` (flow events)
- Modify: `scripts/migrations/migrate_to_postgres.py:479-507` (UW stats)
- Test: `scripts/tests/test_migrate_to_postgres.py` (new)

- [ ] **Step 1: Guard duckdb import (T3)**

In `scripts/migrations/migrate_to_postgres.py`, replace the top-level `import duckdb` at line 16:

```python
# Before:
import duckdb

# After:
try:
    import duckdb
except ImportError:
    duckdb = None
```

Then at each call site that uses `duckdb.connect(...)` (lines ~55, ~110, ~180, ~280), wrap with:

```python
if duckdb is None:
    logger.warning("duckdb not installed — skipping DuckDB migration phase")
else:
    # existing duckdb logic
```

- [ ] **Step 2: Fix UW flow events dict handling (T1)**

In `scripts/migrations/migrate_to_postgres.py:451-453`, replace:

```python
# Before:
events_list = data.get("events", data) if isinstance(data, dict) else data
if not isinstance(events_list, list):
    events_list = []

# After:
events_raw = data.get("events", data) if isinstance(data, dict) else data
if isinstance(events_raw, dict):
    events_list = list(events_raw.values())
    # Inject the dict key as the event id for flow_event_key
    for key, evt in events_raw.items():
        evt.setdefault("id", key)
elif isinstance(events_raw, list):
    events_list = events_raw
else:
    events_list = []
```

Also add `flow_event_key` to the INSERT at line 456:

```python
conn.execute(
    text("""
    INSERT INTO xenon.uw_flow_events
        (flow_event_key, ticker, side, strike, expiry, detected_at, initial,
         daily_track, status, anomaly_reason, closed_at)
    VALUES (:key, :ticker, :side, :strike, :expiry, :detected_at,
            CAST(:initial AS jsonb), CAST(:track AS jsonb), :status,
            :reason, :closed_at)
    ON CONFLICT (flow_event_key) DO NOTHING
    """),
    {
        "key": evt.get("id"),
        "ticker": evt.get("ticker", ""),
        ...  # rest unchanged
    },
)
```

- [ ] **Step 3: Fix UW stats history key and type (T2)**

In `scripts/migrations/migrate_to_postgres.py:484-486`, replace:

```python
# Before:
buckets = data.get("hourly_buckets", data) if isinstance(data, dict) else data
if not isinstance(buckets, list):
    buckets = []
for bucket in buckets:
    ts = bucket.get("timestamp") or bucket.get("bucket_hour")

# After:
buckets = data.get("buckets", {}) if isinstance(data, dict) else {}
if not isinstance(buckets, dict):
    buckets = {}
for hour_key, bucket in buckets.items():
    ts = hour_key  # the dict key IS the timestamp
```

Update the parameter dict to match the live writer's field names (`requests_2xx`, `requests_4xx`, `requests_5xx`, `cached`, `sum_latency_ms`, `latency_count`):

```python
{
    "hour": ts,
    "req": int(bucket.get("requests_2xx", 0))
         + int(bucket.get("requests_4xx", 0))
         + int(bucket.get("requests_5xx", 0)),
    "cache": int(bucket.get("cached", 0)),
    "lat_sum": float(bucket.get("sum_latency_ms", 0.0)),
    "lat_count": int(bucket.get("latency_count", 0)),
    "s2xx": int(bucket.get("requests_2xx", 0)),
    "s4xx": int(bucket.get("requests_4xx", 0)),
    "s5xx": int(bucket.get("requests_5xx", 0)),
}
```

- [ ] **Step 4: Add ON CONFLICT guards to remaining INSERT statements (T4)**

Add `ON CONFLICT DO NOTHING` to the INSERT for these tables (they currently lack it):

- `positions` — no natural unique key, add `ON CONFLICT DO NOTHING` is not useful. Instead wrap entire positions migration in a sentinel check: skip if `positions` has rows.
- `trades` — same pattern, sentinel check.
- `account_snapshots` — same.
- `scan_results` — same.
- `cri_series` — same.
- `uw_analyze_snapshots` — same.
- `uw_flow_events` — already fixed in Step 2 with `ON CONFLICT (flow_event_key) DO NOTHING`.

For tables without a natural unique constraint, add a guard at the top of each migration phase:

```python
existing = conn.execute(text("SELECT count(*) FROM xenon.positions")).scalar()
if existing > 0:
    logger.info("positions already populated (%d rows), skipping", existing)
else:
    # ... migration inserts
```

- [ ] **Step 5: Write test for migration data parsing**

Create `scripts/tests/test_migrate_to_postgres.py`:

```python
"""Unit tests for migration script data parsing — no real DB needed."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_flow_events_dict_format():
    """Flow log JSON with {id: event} dict must parse all events."""
    from scripts.migrations.migrate_to_postgres import _parse_flow_events

    data = {
        "events": {
            "evt-001": {"ticker": "AAPL", "side": "call", "strike": 180.0,
                        "detected_at": "2026-04-20T10:00:00Z",
                        "initial": {"premium_usd": 5000}, "status": "open"},
            "evt-002": {"ticker": "TSLA", "side": "put", "strike": 250.0,
                        "detected_at": "2026-04-21T11:00:00Z",
                        "initial": {"premium_usd": 3000}, "status": "closed"},
        }
    }
    events = _parse_flow_events(data)
    assert len(events) == 2
    ids = {e["id"] for e in events}
    assert ids == {"evt-001", "evt-002"}


def test_uw_stats_buckets_dict_format():
    """UW stats JSON with {hour: bucket} dict must parse all buckets."""
    from scripts.migrations.migrate_to_postgres import _parse_uw_stats_buckets

    data = {
        "buckets": {
            "2026-04-20T10:00:00Z": {"requests_2xx": 100, "requests_4xx": 5,
                                      "requests_5xx": 1, "cached": 20,
                                      "sum_latency_ms": 500.0, "latency_count": 106},
        }
    }
    buckets = _parse_uw_stats_buckets(data)
    assert len(buckets) == 1
    assert buckets[0]["hour"] == "2026-04-20T10:00:00Z"
    assert buckets[0]["req"] == 106


def test_duckdb_import_guard():
    """Migration doesn't crash when duckdb is not installed."""
    import scripts.migrations.migrate_to_postgres as mod
    # Module should load even if duckdb is None
    assert hasattr(mod, "duckdb") or mod.duckdb is None
```

To make these tests work, extract helper functions `_parse_flow_events(data) -> list[dict]` and `_parse_uw_stats_buckets(data) -> list[dict]` from the migration script. This keeps the parsing testable without a DB.

- [ ] **Step 6: Run tests**

```bash
uv run pytest scripts/tests/test_migrate_to_postgres.py -xvs
```

- [ ] **Step 7: Commit**

```bash
git add scripts/migrations/migrate_to_postgres.py scripts/tests/test_migrate_to_postgres.py
git commit -m "fix(migration): fix data-loss bugs in UW flow/stats parsing, guard duckdb import"
```

---

### Task 2: Centralize sync engine — eliminate 5 private singletons (T5, T6, T7)

**Files:**

- Modify: `src/xenon/db/engine.py:39-54` (dispose sync, add URL normalizer)
- Modify: `src/xenon/execution/orders_store.py:26-39` (remove private engine)
- Modify: `src/xenon/execution/ib_sync.py:40-53` (remove private engine)
- Modify: `src/xenon/execution/ib_execute.py:54-67` (remove private engine)
- Modify: `src/xenon/utils/uw_api_stats.py:462-498,547-564` (use shared engine)
- Modify: `src/xenon/api/services/uw_analyze_flow_tracker.py:399-415,502-560` (use shared engine)
- Test: `src/xenon/db/tests/test_engine.py` (extend)

- [ ] **Step 1: Add URL normalizer and fix dispose in engine.py**

In `src/xenon/db/engine.py`, add a URL normalizer and fix `dispose_engine`:

```python
import re

def _normalize_pg_url(url: str, *, driver: str = "asyncpg") -> str:
    """Normalize any postgresql:// URL to the requested driver dialect."""
    # Strip any existing driver suffix
    base = re.sub(r"^postgresql(\+\w+)?://", "postgresql://", url)
    if driver == "asyncpg":
        return base.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif driver == "psycopg":
        return base.replace("postgresql://", "postgresql+psycopg://", 1)
    return base
```

Update `create_engine` to use normalizer:

```python
def create_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    resolved = url or os.environ.get("DATABASE_URL")
    if not resolved:
        raise RuntimeError("DATABASE_URL not set and no url provided")
    async_url = _normalize_pg_url(resolved, driver="asyncpg")
    defaults = { ... }
    defaults.update(kwargs)
    return create_async_engine(async_url, **defaults)
```

Update `get_sync_engine` to use normalizer:

```python
def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL not set")
        sync_url = _normalize_pg_url(url, driver="psycopg")
        _sync_engine = _create_sync_engine(sync_url, pool_pre_ping=True)
    return _sync_engine
```

Fix `dispose_engine`:

```python
async def dispose_engine() -> None:
    global _engine, _sync_engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    if _sync_engine is not None:
        _sync_engine.dispose()
        _sync_engine = None
```

- [ ] **Step 2: Write test for URL normalizer**

In `src/xenon/db/tests/test_engine.py`, add:

```python
from xenon.db.engine import _normalize_pg_url

def test_normalize_plain_to_asyncpg():
    assert _normalize_pg_url("postgresql://u:p@h/db", driver="asyncpg") == "postgresql+asyncpg://u:p@h/db"

def test_normalize_asyncpg_to_psycopg():
    assert _normalize_pg_url("postgresql+asyncpg://u:p@h/db", driver="psycopg") == "postgresql+psycopg://u:p@h/db"

def test_normalize_psycopg_to_asyncpg():
    assert _normalize_pg_url("postgresql+psycopg://u:p@h/db", driver="asyncpg") == "postgresql+asyncpg://u:p@h/db"

def test_normalize_plain_to_psycopg():
    assert _normalize_pg_url("postgresql://u:p@h/db", driver="psycopg") == "postgresql+psycopg://u:p@h/db"
```

- [ ] **Step 3: Run normalizer tests**

```bash
uv run pytest src/xenon/db/tests/test_engine.py -xvs
```

- [ ] **Step 4: Replace private engines in orders_store.py**

In `src/xenon/execution/orders_store.py`, remove lines 20-39 (private engine) and replace with:

```python
from xenon.db.engine import get_sync_engine

# Remove: _pg_engine, _get_pg_engine, create_engine import
# Replace all `_get_pg_engine()` calls with `get_sync_engine()`
```

Every `engine = _get_pg_engine()` (lines 107, 176, 221, 256, 275, 300+) becomes `engine = get_sync_engine()`.

- [ ] **Step 5: Replace private engines in ib_sync.py**

In `src/xenon/execution/ib_sync.py`, remove lines 40-53 (private `_sync_engine` + `_get_sync_engine`). Replace:

```python
from xenon.db.engine import get_sync_engine

# Replace all `_get_sync_engine()` calls with `get_sync_engine()`
```

Affects line 1068 and the `_save_portfolio_to_postgres` function.

- [ ] **Step 6: Replace private engines in ib_execute.py**

Same pattern — remove lines 54-67, import `get_sync_engine`, replace call at line 337.

- [ ] **Step 7: Replace create-and-dispose in uw_api_stats.py**

In `src/xenon/utils/uw_api_stats.py`, at lines 469-475 and 553-559, replace:

```python
# Before:
from sqlalchemy import create_engine as _cse
sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
engine = _cse(sync_url)
# ... work ...
engine.dispose()

# After:
from xenon.db.engine import get_sync_engine
engine = get_sync_engine()
# ... work ... (no dispose — shared pool)
```

Remove the `engine.dispose()` calls at lines 498 and 564.

- [ ] **Step 8: Replace create-and-dispose in uw_analyze_flow_tracker.py**

Same pattern at lines 404-415 and 502-560. Replace with `get_sync_engine()`, remove `engine.dispose()`.

- [ ] **Step 9: Update test conftest to reset only the centralized engine**

In `scripts/tests/conftest.py:56-61` and `src/xenon/api/tests/conftest.py:22-27`, the `_pg_engine` reset on `orders_store_mod` is no longer needed. Replace:

```python
# Before:
import xenon.db.engine as engine_mod
import xenon.execution.orders_store as orders_store_mod
monkeypatch.setattr(engine_mod, "_sync_engine", None)
monkeypatch.setattr(orders_store_mod, "_pg_engine", None)

# After:
import xenon.db.engine as engine_mod
monkeypatch.setattr(engine_mod, "_sync_engine", None)
```

- [ ] **Step 10: Run full test suite**

```bash
uv run pytest -x
```

- [ ] **Step 11: Commit**

```bash
git add src/xenon/db/engine.py src/xenon/db/tests/test_engine.py \
  src/xenon/execution/orders_store.py src/xenon/execution/ib_sync.py \
  src/xenon/execution/ib_execute.py src/xenon/utils/uw_api_stats.py \
  src/xenon/api/services/uw_analyze_flow_tracker.py \
  scripts/tests/conftest.py src/xenon/api/tests/conftest.py
git commit -m "refactor(db): centralize sync engine, eliminate 5 private singletons"
```

---

### Task 3: Fix test infrastructure (T8, T9)

**Files:**

- Modify: `src/xenon/api/tests/conftest.py:14-19` (fix DATABASE_URL dialect)
- Modify: `scripts/tests/conftest.py:35-44` (add missing tables)
- Modify: `src/xenon/api/tests/conftest.py:34-43` (add missing tables)

- [ ] **Step 1: Fix DATABASE_URL dialect in api conftest (T8)**

In `src/xenon/api/tests/conftest.py:14-19`, change:

```python
# Before:
url = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
monkeypatch.setenv("DATABASE_URL", sync_url)

# After:
url = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
monkeypatch.setenv("DATABASE_URL", url)  # keep asyncpg format for init_engine
sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
```

Use `sync_url` only for the truncation engine, not for `DATABASE_URL`.

- [ ] **Step 2: Add missing tables to truncation in both conftest files (T9)**

In both `scripts/tests/conftest.py:35-44` and `src/xenon/api/tests/conftest.py:34-43`, expand the table list:

```python
for table in (
    "xenon.order_events",
    "xenon.order_submissions",
    "xenon.wizard_protection",
    "xenon.wizard_events",
    "xenon.wizard_combo_attempts",
    "xenon.wizard_sessions",
    "xenon.uw_flow_events",
    "xenon.uw_api_stats",
    "xenon.positions",
    "xenon.account_snapshots",
    "xenon.trades",
    "xenon.nav_history",
    "xenon.scan_results",
    "xenon.cri_series",
    "xenon.uw_analyze_snapshots",
    "xenon.ticker_cache",
    "events.outbox",
):
    conn.execute(text(f"TRUNCATE {table} CASCADE"))
```

Order matters: child tables (with FK) before parent. The `CASCADE` handles this but list children first for clarity.

- [ ] **Step 3: Run test suite**

```bash
uv run pytest -x
```

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/conftest.py src/xenon/api/tests/conftest.py
git commit -m "fix(test): fix DATABASE_URL dialect, add all tables to truncation"
```

---

### Task 4: Fix race conditions — register_from_snapshot + apply_modify (T10, T19)

**Files:**

- Modify: `src/xenon/execution/orders_store.py:202-248` (register_from_snapshot)
- Modify: `src/xenon/execution/orders_store.py:250-263` (apply_modify_by_perm_id)
- Test: `scripts/tests/test_orders_store_register_from_snapshot.py` (extend)

- [ ] **Step 1: Fix register_from_snapshot with ON CONFLICT (T10)**

Replace select-then-insert (lines 222-227) with:

```python
def register_from_snapshot(...) -> bool:
    submission_id = f"snapshot-{perm_id}"
    client_attempt_id = f"snapshot-{perm_id}"
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            pg_insert(order_submissions)
            .values(
                submission_id=submission_id,
                user_id=user_id,
                client_attempt_id=client_attempt_id,
                ticker=ticker,
                security_type=security_type,
                action=action,
                quantity=quantity,
                limit_price=Decimal(str(round(limit_price, 4))),
                multiplier=multiplier,
                state="FILLED",
                submitted_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["submission_id"])
            .returning(order_submissions.c.submission_id)
        )
        return result.first() is not None
```

Add import: `from sqlalchemy.dialects.postgresql import insert as pg_insert`

- [ ] **Step 2: Fix apply_modify_by_perm_id — single connection (T19)**

Replace lines 250-263:

```python
def apply_modify_by_perm_id(perm_id: str, sequence: int, db_path=None) -> dict:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.ib_order_id).where(
                order_submissions.c.perm_id == str(perm_id)
            )
        ).first()
        if row is None or not row[0]:
            return {"applied": False, "current_sequence": -1}
        ib_order_id = str(row[0])
        # Inline the apply_modify logic within same transaction
        cur = conn.execute(
            select(order_submissions.c.modify_sequence).where(
                order_submissions.c.ib_order_id == ib_order_id
            )
        ).first()
        if cur is None:
            return {"applied": False, "current_sequence": -1}
        current = int(cur[0])
        if sequence <= current:
            return {"applied": False, "current_sequence": current}
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.ib_order_id == ib_order_id)
            .values(modify_sequence=sequence, updated_at=datetime.now(timezone.utc))
        )
        conn.execute(
            insert(order_events).values(
                submission_id=conn.execute(
                    select(order_submissions.c.submission_id).where(
                        order_submissions.c.ib_order_id == ib_order_id
                    )
                ).scalar(),
                kind="MODIFY_SEQ_ADVANCE",
                detail={"from": current, "to": sequence},
            )
        )
        return {"applied": True, "current_sequence": sequence}
```

- [ ] **Step 3: Write test for concurrent register_from_snapshot**

In `scripts/tests/test_orders_store_register_from_snapshot.py`, add:

```python
def test_register_from_snapshot_concurrent_idempotent():
    """Two calls with same perm_id: first returns True, second False, no exception."""
    from xenon.execution.orders_store import register_from_snapshot
    r1 = register_from_snapshot(
        perm_id="RACE-1", ib_order_id="999", ticker="SPY",
        security_type="OPT", action="BUY", quantity=1, limit_price=5.0,
    )
    r2 = register_from_snapshot(
        perm_id="RACE-1", ib_order_id="999", ticker="SPY",
        security_type="OPT", action="BUY", quantity=1, limit_price=5.0,
    )
    assert r1 is True
    assert r2 is False
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest scripts/tests/test_orders_store_register_from_snapshot.py -xvs
uv run pytest scripts/tests/test_orders_store_modify_sequence.py -xvs
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/orders_store.py scripts/tests/test_orders_store_register_from_snapshot.py
git commit -m "fix(db): eliminate race conditions in register_from_snapshot and apply_modify_by_perm_id"
```

---

### Task 5: Schema constraint hardening — Alembic migration (T11, T12, T13, T14, T15)

**Files:**

- Modify: `src/xenon/db/schema.py` (add constraints + indexes)
- Create: `src/xenon/db/migrations/versions/XXXX_add_constraints_and_indexes.py` (Alembic)
- Modify: `src/xenon/db/events.py:16-26` (validate channel)

- [ ] **Step 1: Update schema.py with constraints and indexes**

Add UNIQUE constraint on `wizard_protection.session_id` (T11):

```python
wizard_protection = Table(
    "wizard_protection",
    xenon_metadata,
    ...
    UniqueConstraint("session_id", name="uq_wizard_protection_session"),
)
```

Add index on `order_events.submission_id` (T15):

```python
order_events = Table(
    ...
    Index("ix_order_events_submission_at", "submission_id", "at"),
)
```

Add index on `wizard_combo_attempts(session_id, updated_at)` (T15):

```python
wizard_combo_attempts = Table(
    ...
    Index("ix_wizard_attempts_session_updated", "session_id", "updated_at"),
)
```

Add index on `wizard_events(session_id, at)` (T15):

```python
# Add if wizard_events has session_id + timestamp columns queried together
```

Add check constraint on `outbox.channel` (T12):

```python
outbox = Table(
    ...
    Column("channel", Text, nullable=False),
    CheckConstraint("length(channel) <= 63 AND channel ~ '^[a-z_][a-z0-9_]*$'", name="ck_outbox_channel_valid"),
)
```

Add import for `CheckConstraint` at the top of schema.py.

- [ ] **Step 2: Validate channel in events.emit() (T12)**

In `src/xenon/db/events.py:16-26`, add validation:

```python
import re

_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

async def emit(conn: AsyncConnection, *, channel: str, source: str, payload: dict) -> int:
    if not _CHANNEL_RE.match(channel):
        raise ValueError(f"Invalid NOTIFY channel: {channel!r} (must be 1-63 lowercase alphanumeric/underscore)")
    result = await conn.execute(
        insert(outbox).values(channel=channel, source=source, payload=payload).returning(outbox.c.id)
    )
    return result.scalar()
```

- [ ] **Step 3: Generate Alembic migration**

```bash
uv run alembic revision --autogenerate -m "add constraints and indexes from review"
```

Review the generated migration. It should contain:

- `CREATE UNIQUE INDEX uq_wizard_protection_session ON xenon.wizard_protection (session_id)`
- `CREATE INDEX ix_order_events_submission_at ON xenon.order_events (submission_id, at)`
- `CREATE INDEX ix_wizard_attempts_session_updated ON xenon.wizard_combo_attempts (session_id, updated_at)`
- `ALTER TABLE events.outbox ADD CONSTRAINT ck_outbox_channel_valid CHECK (...)`

- [ ] **Step 4: Run migration**

```bash
uv run alembic upgrade head
```

- [ ] **Step 5: Handle T13 (nullable idempotency columns) — assess risk**

`order_submissions.user_id` and `client_attempt_id` are nullable for the `register_from_snapshot` path (which sets `user_id="snapshot"`). Routes that create real orders always provide both. The `uq_order_sub_user_attempt` constraint is defense-in-depth for the route path where both are always non-null. The snapshot path uses `submission_id` PK for uniqueness.

**Decision:** Leave nullable. The unique constraint works for its intended purpose (route-created orders). Document this in a code comment.

- [ ] **Step 6: Handle T14 (flow_event_key nullable) — make non-null after backfill**

In the same Alembic migration, if all existing rows have a `flow_event_key`:

```sql
UPDATE xenon.uw_flow_events SET flow_event_key = 'auto-' || id WHERE flow_event_key IS NULL;
ALTER TABLE xenon.uw_flow_events ALTER COLUMN flow_event_key SET NOT NULL;
```

Also update `save_flow_event()` in `src/xenon/db/queries/uw.py:55-85` to require `flow_event_key`:

```python
async def save_flow_event(
    conn: AsyncConnection,
    *,
    flow_event_key: str,  # now required
    ticker: str,
    ...
```

- [ ] **Step 7: Run tests**

```bash
uv run pytest -x
```

- [ ] **Step 8: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/events.py src/xenon/db/queries/uw.py \
  src/xenon/db/migrations/versions/*.py
git commit -m "fix(db): add schema constraints, indexes, channel validation from review"
```

---

### Task 6: Add retention filter to UW stats load (T17)

**Files:**

- Modify: `src/xenon/utils/uw_api_stats.py:547-565`

- [ ] **Step 1: Add WHERE clause to Postgres load**

In `_load_history_from_postgres`, at line 561, replace:

```python
# Before:
rows = conn.execute(select(uw_stats_table)).fetchall()

# After:
from sqlalchemy import func
cutoff = datetime.now(timezone.utc) - timedelta(hours=96)
rows = conn.execute(
    select(uw_stats_table).where(uw_stats_table.c.bucket_hour >= cutoff)
).fetchall()
```

Add `from datetime import timedelta` to imports if not present.

- [ ] **Step 2: Run tests**

```bash
uv run pytest scripts/tests/test_uw_api_stats_history.py -xvs
```

- [ ] **Step 3: Commit**

```bash
git add src/xenon/utils/uw_api_stats.py
git commit -m "fix(db): add 96h retention filter to UW stats Postgres load"
```

---

### Task 7: Add advisory lock to portfolio snapshot write (T18)

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:1080-1130`

- [ ] **Step 1: Wrap delete-insert in advisory lock**

In `_save_portfolio_to_postgres`, wrap the transaction with a Postgres advisory lock:

```python
def _save_portfolio_to_postgres(portfolio: dict) -> None:
    from sqlalchemy import delete, insert, text
    from xenon.db.schema import account_snapshots, positions

    engine = get_sync_engine()
    with engine.begin() as conn:
        # Advisory lock keyed on a fixed hash — prevents concurrent portfolio refreshes
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('portfolio_sync'))"))
        conn.execute(delete(positions).where(positions.c.account == "IB"))
        # ... rest of insert loop unchanged ...
```

`pg_advisory_xact_lock` is transaction-scoped — auto-released on commit/rollback.

- [ ] **Step 2: Run tests**

```bash
uv run pytest scripts/tests/ -x -k "sync or portfolio"
```

- [ ] **Step 3: Commit**

```bash
git add src/xenon/execution/ib_sync.py
git commit -m "fix(db): add advisory lock to portfolio snapshot write"
```

---

### Task 8: Fix DSN rewrite in EventSubscriber (cosmetic but correctness)

**Files:**

- Modify: `src/xenon/db/events.py:56`

- [ ] **Step 1: Use centralized normalizer**

```python
# Before:
raw_dsn = self._dsn.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")

# After:
from xenon.db.engine import _normalize_pg_url
raw_dsn = _normalize_pg_url(self._dsn, driver="asyncpg")
# asyncpg raw connection needs postgresql:// without +asyncpg
raw_dsn = raw_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
```

Actually simpler — just strip the driver:

```python
import re
raw_dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", self._dsn)
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest src/xenon/db/tests/test_events.py -xvs
```

- [ ] **Step 3: Commit**

```bash
git add src/xenon/db/events.py
git commit -m "fix(db): use regex for EventSubscriber DSN normalization"
```

---

### Task 9: Remove unused imports + cleanup (T20)

**Files:**

- Modify: `src/xenon/db/queries/combo_wizard.py:9-10`

- [ ] **Step 1: Remove unused imports**

Delete lines 9-10:

```python
import json    # ← remove
import uuid    # ← remove
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest src/xenon/db/tests/test_combo_wizard.py -xvs
```

- [ ] **Step 3: Run full suite to confirm no regressions**

```bash
uv run pytest -x
```

- [ ] **Step 4: Commit**

```bash
git add src/xenon/db/queries/combo_wizard.py
git commit -m "chore(db): remove unused imports in combo_wizard queries"
```
