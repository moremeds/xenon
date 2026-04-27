# Postgres Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all JSON file and DuckDB persistence with PostgreSQL, establishing a shared data backbone for Xenon and future services.

**Architecture:** SQLAlchemy Core (async) + asyncpg for the connection pool, Alembic for migrations, LISTEN/NOTIFY + outbox table for reactive cross-service events. Four Postgres schemas (`xenon`, `signals`, `marketdata`, `events`) with role-based access. All portability in a single `DATABASE_URL` env var.

**Tech Stack:** PostgreSQL 17, asyncpg, psycopg[binary], SQLAlchemy Core (async), Alembic

**Spec:** `docs/superpowers/specs/2026-04-26-postgres-migration-design.md`

---

## File Structure

### New Files

```
src/xenon/db/
├── __init__.py            # Re-exports engine, get_conn
├── engine.py              # Async engine + pool from DATABASE_URL
├── schema.py              # All SQLAlchemy Table definitions (xenon.* + events.*)
├── events.py              # LISTEN/NOTIFY + outbox helpers
├── queries/
│   ├── __init__.py
│   ├── portfolio.py       # positions, account_snapshots, nav_history
│   ├── orders.py          # order_submissions, order_events
│   ├── wizard.py          # wizard_sessions, wizard_events
│   ├── trades.py          # trades
│   ├── scans.py           # scan_results, cri_series
│   ├── uw.py              # uw_analyze_snapshots, uw_flow_events, uw_api_stats
│   ├── combo_wizard.py    # wizard_combo_attempts, wizard_protection
│   └── cache.py           # ticker_cache
└── migrations/
    ├── env.py             # Alembic env config
    ├── script.py.mako     # Alembic template
    ├── alembic.ini         # Alembic config (points to DATABASE_URL)
    └── versions/          # Auto-generated migration files
```

### Modified Files

```
pyproject.toml                                    # Add asyncpg, sqlalchemy, alembic; remove duckdb
src/xenon/api/server.py                           # Lifespan: init engine, dispose on shutdown
src/xenon/execution/orders_store.py               # Replace DuckDB with db.queries.orders calls
src/xenon/execution/ib_sync.py                    # Replace atomic_save + JSONL with db.queries.portfolio
src/xenon/execution/ib_execute.py                 # Replace JSON append with db.queries.trades
src/xenon/api/services/uw_analyze_cache.py        # Replace file persist with db.queries.uw
src/xenon/api/services/uw_analyze_flow_tracker.py # Replace JSON save with db.queries.uw
src/xenon/utils/uw_api_stats.py                   # Replace JSON history with db.queries.uw
src/xenon/execution/combo_wizard/store.py         # Replace DuckDB with db.queries.combo_wizard
src/xenon/execution/combo_wizard/session.py       # Replace DuckDB with db.queries
src/xenon/execution/combo_wizard/ib_adapter.py    # Replace DuckDB with db.queries
src/xenon/execution/combo_wizard/rehydrate.py     # Replace DuckDB with db.queries
src/xenon/execution/combo_wizard/protect.py       # Replace DuckDB with db.queries
src/xenon/execution/single_leg_rehydrate.py       # Replace DuckDB with db.queries.orders
src/xenon/api/tests/conftest.py                   # Replace DuckDB isolation with Postgres test db
scripts/tests/conftest.py                         # Add Postgres test db fixture
.env                                              # Add DATABASE_URL
```

### New Test Files

```
src/xenon/db/tests/
├── __init__.py
├── conftest.py            # Shared Postgres test fixtures (engine, clean tables)
├── test_engine.py         # Engine connect/disconnect, pool behavior
├── test_schema.py         # Alembic upgrade/downgrade round-trip
├── test_portfolio.py      # Portfolio query functions
├── test_orders.py         # Order lifecycle query functions
├── test_wizard.py         # Wizard session query functions
├── test_trades.py         # Trade journal query functions
├── test_scans.py          # Scanner results query functions
├── test_uw.py             # UW snapshot/flow/stats query functions
├── test_cache.py          # Ticker cache query functions
└── test_events.py         # LISTEN/NOTIFY + outbox
```

---

## Task 1: Dependencies + Local Postgres

**Files:**

- Modify: `pyproject.toml` (dependencies section)
- Modify: `.env` (add DATABASE_URL)

- [ ] **Step 1: Add Python dependencies**

In `pyproject.toml`, add to `[project.dependencies]`:

```toml
"sqlalchemy[asyncio]>=2.0.30",
"asyncpg>=0.30.0",
"psycopg[binary]>=3.2.0",
"alembic>=1.15.0",
```

Do NOT remove `duckdb` yet — it stays until Task 18.

- [ ] **Step 2: Sync deps**

Run: `uv sync --extra test`
Expected: All new packages install successfully.

- [ ] **Step 3: Ensure local Postgres is running**

Run: `pg_isready -h localhost -p 5432`

If Postgres is not installed:

```bash
brew install postgresql@17
brew services start postgresql@17
```

- [ ] **Step 4: Create database + schemas + roles**

```bash
psql -h localhost -U $(whoami) postgres <<'SQL'
-- Database
CREATE DATABASE xenon_db;

-- Connect to it
\c xenon_db

-- Schemas
CREATE SCHEMA IF NOT EXISTS xenon;
CREATE SCHEMA IF NOT EXISTS signals;
CREATE SCHEMA IF NOT EXISTS marketdata;
CREATE SCHEMA IF NOT EXISTS events;

-- Roles
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xenon_app') THEN
    CREATE ROLE xenon_app WITH LOGIN PASSWORD 'xenon_dev';
  END IF;
END $$;

GRANT USAGE, CREATE ON SCHEMA xenon TO xenon_app;
GRANT USAGE ON SCHEMA signals TO xenon_app;
GRANT USAGE ON SCHEMA marketdata TO xenon_app;
GRANT USAGE, CREATE ON SCHEMA events TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon GRANT ALL ON TABLES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT ALL ON TABLES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon GRANT USAGE ON SEQUENCES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT USAGE ON SEQUENCES TO xenon_app;

-- Test database (same schemas, for pytest)
CREATE DATABASE xenon_test;
\c xenon_test
CREATE SCHEMA IF NOT EXISTS xenon;
CREATE SCHEMA IF NOT EXISTS events;
GRANT USAGE, CREATE ON SCHEMA xenon TO xenon_app;
GRANT USAGE, CREATE ON SCHEMA events TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon GRANT ALL ON TABLES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT ALL ON TABLES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon GRANT USAGE ON SEQUENCES TO xenon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT USAGE ON SEQUENCES TO xenon_app;
SQL
```

- [ ] **Step 5: Add DATABASE_URL to .env**

Append to `.env` (root):

```
DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_db
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test
```

- [ ] **Step 6: Commit**

Do NOT commit .env — it contains secrets. DATABASE_URL is documented in the spec.

```bash
git add pyproject.toml uv.lock
git commit -m "feat(db): add sqlalchemy + asyncpg + alembic dependencies"
```

---

## Task 2: Database Engine Module

**Files:**

- Create: `src/xenon/db/__init__.py`
- Create: `src/xenon/db/engine.py`
- Create: `src/xenon/db/tests/__init__.py`
- Create: `src/xenon/db/tests/conftest.py`
- Create: `src/xenon/db/tests/test_engine.py`

- [ ] **Step 1: Write failing test for engine creation**

```python
# src/xenon/db/tests/test_engine.py
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_create_engine_returns_async_engine(pg_url):
    from xenon.db.engine import create_engine

    engine = create_engine(pg_url)
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_engine_can_connect(pg_url):
    from xenon.db.engine import create_engine

    engine = create_engine(pg_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
    await engine.dispose()
```

```python
# src/xenon/db/tests/conftest.py
import os
import pytest


@pytest.fixture
def pg_url():
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url
```

```python
# src/xenon/db/tests/__init__.py
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/db/tests/test_engine.py -xvs`
Expected: FAIL — `ModuleNotFoundError: No module named 'xenon.db'`

- [ ] **Step 3: Implement engine module**

```python
# src/xenon/db/__init__.py
from xenon.db.engine import create_engine, get_engine

__all__ = ["create_engine", "get_engine"]
```

```python
# src/xenon/db/engine.py
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def create_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    resolved = url or os.environ.get("DATABASE_URL")
    if not resolved:
        raise RuntimeError("DATABASE_URL not set and no url provided")
    defaults = {
        "pool_size": 10,
        "max_overflow": 5,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }
    defaults.update(kwargs)
    return create_async_engine(resolved, **defaults)


def init_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    global _engine
    _engine = create_engine(url, **kwargs)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine() first")
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/xenon/db/tests/test_engine.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/
git commit -m "feat(db): add async engine module with connection pooling"
```

---

## Task 3: SQLAlchemy Table Definitions

**Files:**

- Create: `src/xenon/db/schema.py`
- Create: `src/xenon/db/tests/test_schema.py`

- [ ] **Step 1: Write failing test for schema introspection**

```python
# src/xenon/db/tests/test_schema.py
import pytest
from xenon.db.schema import xenon_metadata, events_metadata


def test_xenon_metadata_has_expected_tables():
    expected = {
        "positions", "account_snapshots", "trades", "nav_history",
        "order_submissions", "order_events",
        "wizard_sessions", "wizard_events",
        "wizard_combo_attempts", "wizard_protection",
        "scan_results", "cri_series",
        "uw_analyze_snapshots", "uw_flow_events", "uw_api_stats",
        "ticker_cache",
    }
    actual = set(xenon_metadata.tables.keys())
    # Table keys include schema prefix
    actual_names = {name.split(".")[-1] for name in actual}
    assert expected.issubset(actual_names), f"Missing: {expected - actual_names}"


def test_events_metadata_has_outbox():
    actual = set(events_metadata.tables.keys())
    actual_names = {name.split(".")[-1] for name in actual}
    assert "outbox" in actual_names


def test_order_submissions_has_required_columns():
    table = xenon_metadata.tables["xenon.order_submissions"]
    col_names = {c.name for c in table.columns}
    required = {
        "submission_id", "user_id", "ticker", "security_type",
        "action", "quantity", "state", "submitted_at", "updated_at",
    }
    assert required.issubset(col_names), f"Missing: {required - col_names}"


def test_positions_has_account_column():
    table = xenon_metadata.tables["xenon.positions"]
    col_names = {c.name for c in table.columns}
    assert "account" in col_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/db/tests/test_schema.py -xvs`
Expected: FAIL — `ImportError: cannot import name 'xenon_metadata'`

- [ ] **Step 3: Implement schema definitions**

```python
# src/xenon/db/schema.py
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

XENON_SCHEMA = "xenon"
EVENTS_SCHEMA = "events"

xenon_metadata = MetaData(schema=XENON_SCHEMA)
events_metadata = MetaData(schema=EVENTS_SCHEMA)

tz_now = text("now()")

# ---------- Portfolio & Trading ----------

positions = Table(
    "positions",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("security_type", Text, nullable=False),
    Column("expiry", Date),
    Column("strike", Numeric(12, 2)),
    Column("right", Text),
    Column("quantity", Integer, nullable=False),
    Column("avg_cost", Numeric(12, 4), nullable=False),
    Column("current_price", Numeric(12, 4)),
    Column("unrealized_pnl", Numeric(12, 2)),
    Column("account", Text, nullable=False),
    Column("synced_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

account_snapshots = Table(
    "account_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("account", Text, nullable=False),
    Column("bankroll", Numeric(14, 2), nullable=False),
    Column("peak_value", Numeric(14, 2)),
    Column("net_liquidation", Numeric(14, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

trades = Table(
    "trades",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("structure", Text),
    Column("action", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("entry_cost", Numeric(12, 4)),
    Column("exit_cost", Numeric(12, 4)),
    Column("realized_pnl", Numeric(12, 2)),
    Column("edge", Text),
    Column("decision", Text),
    Column("opened_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("metadata", JSONB),
)

nav_history = Table(
    "nav_history",
    xenon_metadata,
    Column("date", Date, primary_key=True),
    Column("nav", Numeric(14, 2), nullable=False),
    Column("daily_pnl", Numeric(12, 2)),
)

# ---------- Order Lifecycle ----------

order_submissions = Table(
    "order_submissions",
    xenon_metadata,
    Column("submission_id", Text, primary_key=True),
    Column("user_id", Text),
    Column("client_attempt_id", Text),
    Column("ticker", Text, nullable=False),
    Column("security_type", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("expiry", Date),
    Column("strike", Numeric(12, 2)),
    Column("right", Text),
    Column("multiplier", Integer, server_default=text("100")),
    Column("con_id", BigInteger),
    Column("placing_client_id", Integer),
    Column("ib_order_id", Text),
    Column("perm_id", Text),
    Column("limit_price", Numeric(12, 4)),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
    Column("filled_qty", Integer, server_default=text("0")),
    Column("avg_fill_price", Numeric(12, 4)),
    Column("modify_sequence", Integer, server_default=text("0")),
    Column("submitted_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    UniqueConstraint("user_id", "client_attempt_id", name="uq_order_sub_user_attempt"),
    Index("ix_order_sub_state_ticker", "state", "ticker"),
    Index("ix_order_sub_perm_id", "perm_id"),
    Index("ix_order_sub_ib_order_id", "ib_order_id"),
)

order_events = Table(
    "order_events",
    xenon_metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column("submission_id", Text, ForeignKey(f"{XENON_SCHEMA}.order_submissions.submission_id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

wizard_sessions = Table(
    "wizard_sessions",
    xenon_metadata,
    Column("session_id", Text, primary_key=True),
    Column("ticker", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("structure_name", Text),
    Column("intent", Text),
    Column("payload", JSONB),
    Column("current_attempt_id", Text),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

wizard_events = Table(
    "wizard_events",
    xenon_metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column("session_id", Text, ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

wizard_combo_attempts = Table(
    "wizard_combo_attempts",
    xenon_metadata,
    Column("attempt_id", Text, primary_key=True),
    Column("session_id", Text, ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"), nullable=False),
    Column("ticker", Text, nullable=False),
    Column("structure_name", Text),
    Column("legs", JSONB),
    Column("combo_contract", JSONB),
    Column("ib_order_id", Text),
    Column("perm_id", Text),
    Column("placing_client_id", Integer),
    Column("limit_price", Numeric(12, 4)),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
    Column("filled_qty", Integer, server_default=text("0")),
    Column("avg_fill_price", Numeric(12, 4)),
    Column("modify_sequence", Integer, server_default=text("0")),
    Column("submitted_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

wizard_protection = Table(
    "wizard_protection",
    xenon_metadata,
    Column("protection_id", BigInteger, primary_key=True, autoincrement=True),
    Column("session_id", Text, ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"), nullable=False),
    Column("attempt_id", Text, ForeignKey(f"{XENON_SCHEMA}.wizard_combo_attempts.attempt_id")),
    Column("protection_type", Text, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("triggered_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

# ---------- Scanner Results ----------

scan_results = Table(
    "scan_results",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scan_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

cri_series = Table(
    "cri_series",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("cri_level", Numeric(8, 4), nullable=False),
    Column("alert", Boolean, server_default=text("false")),
    Column("payload", JSONB),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

# ---------- UW Analysis ----------

uw_analyze_snapshots = Table(
    "uw_analyze_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("vrp_state", JSONB),
    Column("regime", JSONB),
    Column("flow_signals", JSONB),
    Column("portfolio_score", Numeric(6, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Index("ix_uw_snap_ticker_time", "ticker", "snapshot_at"),
)

uw_flow_events = Table(
    "uw_flow_events",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("side", Text),
    Column("strike", Numeric(12, 2)),
    Column("expiry", Date),
    Column("detected_at", TIMESTAMP(timezone=True), nullable=False),
    Column("initial", JSONB, nullable=False),
    Column("daily_track", JSONB),
    Column("status", Text, nullable=False),
    Column("anomaly_reason", Text),
    Column("closed_at", TIMESTAMP(timezone=True)),
)

uw_api_stats = Table(
    "uw_api_stats",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bucket_hour", TIMESTAMP(timezone=True), nullable=False, unique=True),
    Column("requests", Integer, server_default=text("0")),
    Column("cache_hits", Integer, server_default=text("0")),
    Column("latency_sum", Numeric(10, 2), server_default=text("0")),
    Column("latency_count", Integer, server_default=text("0")),
    Column("status_2xx", Integer, server_default=text("0")),
    Column("status_4xx", Integer, server_default=text("0")),
    Column("status_5xx", Integer, server_default=text("0")),
)

# ---------- Caches ----------

ticker_cache = Table(
    "ticker_cache",
    xenon_metadata,
    Column("ticker", Text, nullable=False, primary_key=True),
    Column("cache_type", Text, nullable=False, primary_key=True),
    Column("data", JSONB, nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

# ---------- Shared Event Bus ----------

outbox = Table(
    "outbox",
    events_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("channel", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("emitted_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("consumed_by", JSONB, server_default=text("'[]'::jsonb")),
    Index("ix_outbox_channel_time", "channel", "emitted_at"),
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/xenon/db/tests/test_schema.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/tests/test_schema.py
git commit -m "feat(db): define all SQLAlchemy table schemas for xenon + events"
```

---

## Task 4: Alembic Setup + Initial Migration

**Files:**

- Create: `src/xenon/db/migrations/env.py`
- Create: `src/xenon/db/migrations/script.py.mako`
- Create: `alembic.ini` (project root)
- Create: `src/xenon/db/migrations/versions/` (directory)

- [ ] **Step 1: Initialize Alembic**

```bash
cd /Users/chenxi/projects/xenon
uv run alembic init src/xenon/db/migrations
```

This creates `alembic.ini` at root and `src/xenon/db/migrations/` with env.py, script.py.mako, versions/.

- [ ] **Step 2: Configure alembic.ini**

Edit `alembic.ini` — set `script_location` and `sqlalchemy.url`:

```ini
[alembic]
script_location = src/xenon/db/migrations
sqlalchemy.url = postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_db
```

- [ ] **Step 3: Configure env.py for async + multi-schema**

Replace `src/xenon/db/migrations/env.py` with:

```python
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from xenon.db.schema import xenon_metadata, events_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url_override = os.environ.get("DATABASE_URL")
if url_override:
    config.set_main_option("sqlalchemy.url", url_override)

target_metadata = [xenon_metadata, events_metadata]


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in ("xenon", "events")
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema="xenon",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="xenon",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Generate initial migration**

```bash
uv run alembic revision --autogenerate -m "initial schema"
```

Expected: Creates a file in `src/xenon/db/migrations/versions/` with CREATE TABLE statements for all 14 xenon tables + 1 events table.

- [ ] **Step 5: Review the generated migration**

Open the generated file and verify:

- All 14 `xenon.*` tables present
- `events.outbox` table present
- Indexes are included
- Foreign keys reference correct tables

- [ ] **Step 6: Run the migration**

```bash
uv run alembic upgrade head
```

Expected: All tables created in `xenon_db`.

- [ ] **Step 7: Verify tables exist**

```bash
psql -h localhost -U xenon_app xenon_db -c "\dt xenon.*"
psql -h localhost -U xenon_app xenon_db -c "\dt events.*"
```

Expected: 14 xenon tables + 1 events table listed.

- [ ] **Step 8: Run migration on test database too**

```bash
DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test uv run alembic upgrade head
```

- [ ] **Step 9: Commit**

```bash
git add alembic.ini src/xenon/db/migrations/
git commit -m "feat(db): alembic setup with initial migration for all tables"
```

---

## Task 5: Test Infrastructure (conftest for DB tests)

**Files:**

- Modify: `src/xenon/db/tests/conftest.py`

- [ ] **Step 1: Implement shared test fixtures**

```python
# src/xenon/db/tests/conftest.py
import os
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.engine import create_engine
from xenon.db.schema import xenon_metadata, events_metadata


@pytest.fixture
def pg_url():
    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


@pytest_asyncio.fixture
async def engine(pg_url):
    eng = create_engine(pg_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def conn(engine):
    async with engine.begin() as connection:
        yield connection
        await connection.rollback()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine):
    """Truncate all tables before each test for isolation."""
    async with engine.begin() as connection:
        for meta in (xenon_metadata, events_metadata):
            for table in reversed(meta.sorted_tables):
                await connection.execute(
                    text(f"TRUNCATE TABLE {table.schema}.{table.name} CASCADE")
                )
    yield
```

- [ ] **Step 2: Verify fixtures work with existing engine test**

Run: `uv run pytest src/xenon/db/tests/test_engine.py -xvs`
Expected: PASS (fixtures provide pg_url)

- [ ] **Step 3: Commit**

```bash
git add src/xenon/db/tests/conftest.py
git commit -m "feat(db): add shared test fixtures with per-test table truncation"
```

---

## Task 6: Portfolio Queries

**Files:**

- Create: `src/xenon/db/queries/__init__.py`
- Create: `src/xenon/db/queries/portfolio.py`
- Create: `src/xenon/db/tests/test_portfolio.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_portfolio.py
import pytest
from datetime import date, datetime, timezone
from decimal import Decimal


@pytest.mark.asyncio
async def test_save_and_get_positions(conn):
    from xenon.db.queries.portfolio import save_positions, get_positions

    positions = [
        {
            "ticker": "AAPL",
            "security_type": "STK",
            "quantity": 100,
            "avg_cost": Decimal("150.25"),
            "current_price": Decimal("155.00"),
            "unrealized_pnl": Decimal("475.00"),
            "account": "IB",
        },
        {
            "ticker": "SPY",
            "security_type": "OPT",
            "expiry": date(2026, 5, 16),
            "strike": Decimal("520.00"),
            "right": "CALL",
            "quantity": 5,
            "avg_cost": Decimal("12.50"),
            "account": "IB",
        },
    ]
    await save_positions(conn, positions, account="IB")
    result = await get_positions(conn, account="IB")
    assert len(result) == 2
    assert result[0]["ticker"] == "AAPL"
    assert result[1]["ticker"] == "SPY"


@pytest.mark.asyncio
async def test_save_positions_replaces_previous(conn):
    from xenon.db.queries.portfolio import save_positions, get_positions

    await save_positions(conn, [{"ticker": "AAPL", "security_type": "STK", "quantity": 100, "avg_cost": Decimal("150"), "account": "IB"}], account="IB")
    await save_positions(conn, [{"ticker": "MSFT", "security_type": "STK", "quantity": 50, "avg_cost": Decimal("400"), "account": "IB"}], account="IB")
    result = await get_positions(conn, account="IB")
    assert len(result) == 1
    assert result[0]["ticker"] == "MSFT"


@pytest.mark.asyncio
async def test_save_account_snapshot(conn):
    from xenon.db.queries.portfolio import save_account_snapshot, get_latest_snapshot

    await save_account_snapshot(conn, account="IB", bankroll=Decimal("100000"), peak_value=Decimal("105000"), net_liquidation=Decimal("102000"))
    snap = await get_latest_snapshot(conn, account="IB")
    assert snap["bankroll"] == Decimal("100000")


@pytest.mark.asyncio
async def test_upsert_nav(conn):
    from xenon.db.queries.portfolio import upsert_nav, get_nav_history

    today = date(2026, 4, 26)
    await upsert_nav(conn, today, nav=Decimal("100000"), daily_pnl=Decimal("500"))
    await upsert_nav(conn, today, nav=Decimal("100200"), daily_pnl=Decimal("700"))
    history = await get_nav_history(conn)
    assert len(history) == 1
    assert history[0]["nav"] == Decimal("100200")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_portfolio.py -xvs`
Expected: FAIL — `ModuleNotFoundError: No module named 'xenon.db.queries'`

- [ ] **Step 3: Implement portfolio queries**

```python
# src/xenon/db/queries/__init__.py
```

```python
# src/xenon/db/queries/portfolio.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import account_snapshots, nav_history, positions


async def save_positions(
    conn: AsyncConnection, rows: list[dict], *, account: str
) -> None:
    await conn.execute(delete(positions).where(positions.c.account == account))
    if rows:
        await conn.execute(insert(positions), rows)


async def get_positions(
    conn: AsyncConnection, *, account: str | None = None
) -> list[dict]:
    stmt = select(positions)
    if account:
        stmt = stmt.where(positions.c.account == account)
    stmt = stmt.order_by(positions.c.ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def save_account_snapshot(
    conn: AsyncConnection,
    *,
    account: str,
    bankroll: Decimal,
    peak_value: Decimal | None = None,
    net_liquidation: Decimal | None = None,
) -> None:
    await conn.execute(
        insert(account_snapshots).values(
            account=account,
            bankroll=bankroll,
            peak_value=peak_value,
            net_liquidation=net_liquidation,
        )
    )


async def get_latest_snapshot(
    conn: AsyncConnection, *, account: str
) -> dict | None:
    stmt = (
        select(account_snapshots)
        .where(account_snapshots.c.account == account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    row = result.first()
    return dict(row._mapping) if row else None


async def upsert_nav(
    conn: AsyncConnection,
    day: date,
    *,
    nav: Decimal,
    daily_pnl: Decimal | None = None,
) -> None:
    stmt = pg_insert(nav_history).values(date=day, nav=nav, daily_pnl=daily_pnl)
    stmt = stmt.on_conflict_do_update(
        index_elements=[nav_history.c.date],
        set_={"nav": stmt.excluded.nav, "daily_pnl": stmt.excluded.daily_pnl},
    )
    await conn.execute(stmt)


async def get_nav_history(conn: AsyncConnection) -> list[dict]:
    stmt = select(nav_history).order_by(nav_history.c.date)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_portfolio.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/ src/xenon/db/tests/test_portfolio.py
git commit -m "feat(db): portfolio query functions (positions, snapshots, NAV)"
```

---

## Task 7: Order Lifecycle Queries

**Files:**

- Create: `src/xenon/db/queries/orders.py`
- Create: `src/xenon/db/tests/test_orders.py`

This is the most critical migration — it replaces `src/xenon/execution/orders_store.py` (DuckDB). Must match the exact same function signatures and return types the rest of the codebase expects.

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_orders.py
import pytest
from datetime import datetime, timezone
from decimal import Decimal


@pytest.mark.asyncio
async def test_reserve_attempt(conn):
    from xenon.db.queries.orders import reserve_attempt

    result = await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150.00"),
    )
    assert result["submission_id"] == "sub-001"
    assert result["state"] == "PENDING"


@pytest.mark.asyncio
async def test_reserve_attempt_idempotent(conn):
    from xenon.db.queries.orders import reserve_attempt

    r1 = await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    r2 = await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    assert r1["submission_id"] == r2["submission_id"]


@pytest.mark.asyncio
async def test_mark_submitted(conn):
    from xenon.db.queries.orders import reserve_attempt, mark_submitted, get_by_submission_id

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    row = await get_by_submission_id(conn, "sub-001")
    assert row["state"] == "WORKING"
    assert row["ib_order_id"] == 12345


@pytest.mark.asyncio
async def test_mark_terminal(conn):
    from xenon.db.queries.orders import reserve_attempt, mark_submitted, mark_terminal, get_by_submission_id

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    await mark_terminal(conn, submission_id="sub-001", state="FILLED", filled_qty=100, avg_fill_price=Decimal("149.50"))
    row = await get_by_submission_id(conn, "sub-001")
    assert row["state"] == "FILLED"
    assert row["filled_qty"] == 100


@pytest.mark.asyncio
async def test_record_event(conn):
    from xenon.db.queries.orders import reserve_attempt, record_event, get_events

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await record_event(conn, submission_id="sub-001", kind="SUBMITTED", detail={"ib_order_id": 12345})
    events = await get_events(conn, submission_id="sub-001")
    assert len(events) == 1
    assert events[0]["kind"] == "SUBMITTED"


@pytest.mark.asyncio
async def test_lookup_by_perm_id(conn):
    from xenon.db.queries.orders import reserve_attempt, mark_submitted, lookup_by_perm_id

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    sid = await lookup_by_perm_id(conn, 99999)
    assert sid == "sub-001"


@pytest.mark.asyncio
async def test_lookup_by_ib_order_id(conn):
    from xenon.db.queries.orders import reserve_attempt, mark_submitted, lookup_by_ib_order_id

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    sid = await lookup_by_ib_order_id(conn, 12345)
    assert sid == "sub-001"


@pytest.mark.asyncio
async def test_apply_modify(conn):
    from xenon.db.queries.orders import reserve_attempt, apply_modify, get_by_submission_id

    await reserve_attempt(conn, submission_id="sub-001", user_id="user-1", client_attempt_id="att-1", ticker="AAPL", security_type="STK", action="BUY", quantity=100, limit_price=Decimal("150"))
    await apply_modify(conn, submission_id="sub-001", modify_sequence=1)
    row = await get_by_submission_id(conn, "sub-001")
    assert row["modify_sequence"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_orders.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement order queries**

```python
# src/xenon/db/queries/orders.py
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import order_events, order_submissions


async def reserve_attempt(
    conn: AsyncConnection,
    *,
    submission_id: str,
    user_id: str,
    client_attempt_id: str,
    ticker: str,
    security_type: str,
    action: str,
    quantity: int,
    limit_price: Decimal,
    expiry=None,
    strike=None,
    right=None,
    multiplier: int = 100,
    con_id: int | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    values = dict(
        submission_id=submission_id,
        user_id=user_id,
        client_attempt_id=client_attempt_id,
        ticker=ticker,
        security_type=security_type,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=multiplier,
        con_id=con_id,
        state="PENDING",
        submitted_at=now,
        updated_at=now,
    )
    stmt = pg_insert(order_submissions).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["submission_id"])
    await conn.execute(stmt)
    return await get_by_submission_id(conn, submission_id)


async def get_by_submission_id(
    conn: AsyncConnection, submission_id: str
) -> dict | None:
    stmt = select(order_submissions).where(
        order_submissions.c.submission_id == submission_id
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def mark_submitted(
    conn: AsyncConnection,
    *,
    submission_id: str,
    ib_order_id: int,
    perm_id: int,
    placing_client_id: int,
) -> None:
    await conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(
            state="WORKING",
            ib_order_id=ib_order_id,
            perm_id=perm_id,
            placing_client_id=placing_client_id,
            updated_at=datetime.now(timezone.utc),
        )
    )


async def mark_terminal(
    conn: AsyncConnection,
    *,
    submission_id: str,
    state: str,
    reason_code: str | None = None,
    filled_qty: int = 0,
    avg_fill_price: Decimal | None = None,
) -> None:
    await conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(
            state=state,
            reason_code=reason_code,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            updated_at=datetime.now(timezone.utc),
        )
    )


async def apply_modify(
    conn: AsyncConnection, *, submission_id: str, modify_sequence: int
) -> dict:
    result = await conn.execute(
        update(order_submissions)
        .where(
            order_submissions.c.submission_id == submission_id,
            order_submissions.c.modify_sequence < modify_sequence,
        )
        .values(
            modify_sequence=modify_sequence,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(order_submissions.c.modify_sequence)
    )
    row = result.first()
    if row:
        return {"applied": True, "current_sequence": row[0]}
    # Check if submission exists
    existing = await conn.execute(
        select(order_submissions.c.modify_sequence).where(
            order_submissions.c.submission_id == submission_id
        )
    )
    ex_row = existing.first()
    if ex_row:
        return {"applied": False, "current_sequence": ex_row[0]}
    return {"applied": False, "current_sequence": -1}


async def record_event(
    conn: AsyncConnection,
    *,
    submission_id: str,
    kind: str,
    detail: dict | None = None,
) -> None:
    await conn.execute(
        insert(order_events).values(
            submission_id=submission_id, kind=kind, detail=detail
        )
    )


async def get_events(
    conn: AsyncConnection, *, submission_id: str
) -> list[dict]:
    stmt = (
        select(order_events)
        .where(order_events.c.submission_id == submission_id)
        .order_by(order_events.c.at)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def lookup_by_perm_id(
    conn: AsyncConnection, perm_id: int
) -> str | None:
    stmt = select(order_submissions.c.submission_id).where(
        order_submissions.c.perm_id == perm_id
    )
    row = (await conn.execute(stmt)).first()
    return row[0] if row else None


async def lookup_by_ib_order_id(
    conn: AsyncConnection, ib_order_id: int
) -> str | None:
    stmt = select(order_submissions.c.submission_id).where(
        order_submissions.c.ib_order_id == ib_order_id
    )
    row = (await conn.execute(stmt)).first()
    return row[0] if row else None


async def lookup_by_attempt(
    conn: AsyncConnection, user_id: str, client_attempt_id: str
) -> dict | None:
    stmt = select(order_submissions).where(
        order_submissions.c.user_id == user_id,
        order_submissions.c.client_attempt_id == client_attempt_id,
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def working_orders_for(
    conn: AsyncConnection, *, user_id: str, ticker: str
) -> list[dict]:
    stmt = select(order_submissions).where(
        order_submissions.c.user_id == user_id,
        order_submissions.c.ticker == ticker,
        order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED"]),
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_orders.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/orders.py src/xenon/db/tests/test_orders.py
git commit -m "feat(db): order lifecycle query functions"
```

---

## Task 8: Wizard Session Queries

**Files:**

- Create: `src/xenon/db/queries/wizard.py`
- Create: `src/xenon/db/tests/test_wizard.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_wizard.py
import pytest


@pytest.mark.asyncio
async def test_create_and_get_session(conn):
    from xenon.db.queries.wizard import create_session, get_session

    await create_session(conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN")
    sess = await get_session(conn, "ws-001")
    assert sess["ticker"] == "AAPL"
    assert sess["state"] == "planned"


@pytest.mark.asyncio
async def test_update_session_state(conn):
    from xenon.db.queries.wizard import create_session, update_session_state, get_session

    await create_session(conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN")
    await update_session_state(conn, session_id="ws-001", state="pricing", payload={"legs": [{"strike": 150}]})
    sess = await get_session(conn, "ws-001")
    assert sess["state"] == "pricing"
    assert sess["payload"]["legs"][0]["strike"] == 150


@pytest.mark.asyncio
async def test_record_wizard_event(conn):
    from xenon.db.queries.wizard import create_session, record_event, get_events

    await create_session(conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN")
    await record_event(conn, session_id="ws-001", kind="PRICED", detail={"mid": 2.50})
    events = await get_events(conn, session_id="ws-001")
    assert len(events) == 1
    assert events[0]["kind"] == "PRICED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_wizard.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement wizard queries**

```python
# src/xenon/db/queries/wizard.py
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import wizard_events, wizard_sessions


async def create_session(
    conn: AsyncConnection,
    *,
    session_id: str,
    ticker: str,
    state: str,
    structure_name: str | None = None,
    intent: str | None = None,
    payload: dict | None = None,
    current_attempt_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    await conn.execute(
        insert(wizard_sessions).values(
            session_id=session_id,
            ticker=ticker,
            state=state,
            structure_name=structure_name,
            intent=intent,
            payload=payload,
            current_attempt_id=current_attempt_id,
            created_at=now,
            updated_at=now,
        )
    )


async def get_session(conn: AsyncConnection, session_id: str) -> dict | None:
    stmt = select(wizard_sessions).where(
        wizard_sessions.c.session_id == session_id
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def update_session_state(
    conn: AsyncConnection,
    *,
    session_id: str,
    state: str,
    payload: dict | None = None,
    current_attempt_id: str | None = None,
) -> None:
    values: dict = {
        "state": state,
        "updated_at": datetime.now(timezone.utc),
    }
    if payload is not None:
        values["payload"] = payload
    if current_attempt_id is not None:
        values["current_attempt_id"] = current_attempt_id
    await conn.execute(
        update(wizard_sessions)
        .where(wizard_sessions.c.session_id == session_id)
        .values(**values)
    )


async def record_event(
    conn: AsyncConnection,
    *,
    session_id: str,
    kind: str,
    detail: dict | None = None,
) -> None:
    await conn.execute(
        insert(wizard_events).values(
            session_id=session_id, kind=kind, detail=detail
        )
    )


async def get_events(
    conn: AsyncConnection, *, session_id: str
) -> list[dict]:
    stmt = (
        select(wizard_events)
        .where(wizard_events.c.session_id == session_id)
        .order_by(wizard_events.c.at)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_wizard.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/wizard.py src/xenon/db/tests/test_wizard.py
git commit -m "feat(db): wizard session query functions"
```

---

## Task 9: Trade Journal Queries

**Files:**

- Create: `src/xenon/db/queries/trades.py`
- Create: `src/xenon/db/tests/test_trades.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_trades.py
import pytest
from decimal import Decimal
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_append_and_get_trades(conn):
    from xenon.db.queries.trades import append_trade, get_journal

    await append_trade(conn, ticker="AAPL", action="BUY", quantity=100, structure="vertical", entry_cost=Decimal("5.00"), edge="dark_pool_sweep", decision="PASS_ALL_GATES")
    await append_trade(conn, ticker="MSFT", action="SELL", quantity=50, realized_pnl=Decimal("200.00"))
    journal = await get_journal(conn)
    assert len(journal) == 2
    assert journal[0]["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_get_journal_by_ticker(conn):
    from xenon.db.queries.trades import append_trade, get_journal

    await append_trade(conn, ticker="AAPL", action="BUY", quantity=100)
    await append_trade(conn, ticker="MSFT", action="BUY", quantity=50)
    result = await get_journal(conn, ticker="AAPL")
    assert len(result) == 1
    assert result[0]["ticker"] == "AAPL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_trades.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement trade queries**

```python
# src/xenon/db/queries/trades.py
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import trades


async def append_trade(
    conn: AsyncConnection,
    *,
    ticker: str,
    action: str,
    quantity: int,
    structure: str | None = None,
    entry_cost: Decimal | None = None,
    exit_cost: Decimal | None = None,
    realized_pnl: Decimal | None = None,
    edge: str | None = None,
    decision: str | None = None,
    opened_at=None,
    closed_at=None,
    metadata: dict | None = None,
) -> int:
    result = await conn.execute(
        insert(trades)
        .values(
            ticker=ticker,
            action=action,
            quantity=quantity,
            structure=structure,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
            realized_pnl=realized_pnl,
            edge=edge,
            decision=decision,
            opened_at=opened_at,
            closed_at=closed_at,
            metadata=metadata,
        )
        .returning(trades.c.id)
    )
    return result.scalar()


async def get_journal(
    conn: AsyncConnection, *, ticker: str | None = None
) -> list[dict]:
    stmt = select(trades).order_by(trades.c.id)
    if ticker:
        stmt = stmt.where(trades.c.ticker == ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_trades.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/trades.py src/xenon/db/tests/test_trades.py
git commit -m "feat(db): trade journal query functions"
```

---

## Task 10: Scanner Result Queries

**Files:**

- Create: `src/xenon/db/queries/scans.py`
- Create: `src/xenon/db/tests/test_scans.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_scans.py
import pytest
from decimal import Decimal


@pytest.mark.asyncio
async def test_save_and_get_latest_scan(conn):
    from xenon.db.queries.scans import save_scan, get_latest_scan

    await save_scan(conn, scan_type="watchlist", payload={"candidates": [{"ticker": "AAPL", "score": 85}]})
    await save_scan(conn, scan_type="watchlist", payload={"candidates": [{"ticker": "MSFT", "score": 92}]})
    latest = await get_latest_scan(conn, scan_type="watchlist")
    assert latest["payload"]["candidates"][0]["ticker"] == "MSFT"


@pytest.mark.asyncio
async def test_get_latest_scan_nonexistent(conn):
    from xenon.db.queries.scans import get_latest_scan

    result = await get_latest_scan(conn, scan_type="watchlist")
    assert result is None


@pytest.mark.asyncio
async def test_save_cri_datapoint(conn):
    from xenon.db.queries.scans import save_cri_datapoint, get_cri_series

    await save_cri_datapoint(conn, cri_level=Decimal("0.35"), alert=False, payload={"regime": "calm"})
    await save_cri_datapoint(conn, cri_level=Decimal("0.72"), alert=True, payload={"regime": "stress"})
    series = await get_cri_series(conn, limit=10)
    assert len(series) == 2
    assert series[1]["alert"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_scans.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement scan queries**

```python
# src/xenon/db/queries/scans.py
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import cri_series, scan_results


async def save_scan(
    conn: AsyncConnection, *, scan_type: str, payload: dict
) -> None:
    await conn.execute(
        insert(scan_results).values(scan_type=scan_type, payload=payload)
    )


async def get_latest_scan(
    conn: AsyncConnection, *, scan_type: str
) -> dict | None:
    stmt = (
        select(scan_results)
        .where(scan_results.c.scan_type == scan_type)
        .order_by(scan_results.c.scanned_at.desc(), scan_results.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def save_cri_datapoint(
    conn: AsyncConnection,
    *,
    cri_level: Decimal,
    alert: bool = False,
    payload: dict | None = None,
) -> None:
    await conn.execute(
        insert(cri_series).values(
            cri_level=cri_level, alert=alert, payload=payload
        )
    )


async def get_cri_series(
    conn: AsyncConnection, *, limit: int = 100
) -> list[dict]:
    stmt = (
        select(cri_series)
        .order_by(cri_series.c.recorded_at)
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_scans.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/scans.py src/xenon/db/tests/test_scans.py
git commit -m "feat(db): scanner result query functions"
```

---

## Task 11: UW Analysis Queries

**Files:**

- Create: `src/xenon/db/queries/uw.py`
- Create: `src/xenon/db/tests/test_uw.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_uw.py
import pytest
from datetime import date, datetime, timezone
from decimal import Decimal


@pytest.mark.asyncio
async def test_save_and_get_uw_snapshot(conn):
    from xenon.db.queries.uw import save_snapshot, get_latest_snapshot

    await save_snapshot(conn, ticker="AAPL", vrp_state={"iv_rank": 0.65}, regime={"label": "high_vol"}, flow_signals={"sweeps": 3}, portfolio_score=Decimal("7.50"))
    snap = await get_latest_snapshot(conn, ticker="AAPL")
    assert snap["ticker"] == "AAPL"
    assert snap["vrp_state"]["iv_rank"] == 0.65


@pytest.mark.asyncio
async def test_get_snapshot_history(conn):
    from xenon.db.queries.uw import save_snapshot, get_snapshot_history

    await save_snapshot(conn, ticker="AAPL", portfolio_score=Decimal("7.0"))
    await save_snapshot(conn, ticker="AAPL", portfolio_score=Decimal("8.0"))
    history = await get_snapshot_history(conn, ticker="AAPL")
    assert len(history) == 2


@pytest.mark.asyncio
async def test_save_and_get_flow_event(conn):
    from xenon.db.queries.uw import save_flow_event, get_flow_events

    await save_flow_event(conn, ticker="TSLA", side="call", strike=Decimal("250.00"), expiry=date(2026, 5, 16), detected_at=datetime.now(timezone.utc), initial={"premium": 50000, "oi": 1200}, status="open")
    events = await get_flow_events(conn, status="open")
    assert len(events) == 1
    assert events[0]["ticker"] == "TSLA"


@pytest.mark.asyncio
async def test_upsert_api_stats(conn):
    from xenon.db.queries.uw import upsert_api_stats, get_api_stats

    hour = datetime(2026, 4, 26, 14, 0, 0, tzinfo=timezone.utc)
    await upsert_api_stats(conn, bucket_hour=hour, requests=50, cache_hits=30, status_2xx=48, status_4xx=2)
    await upsert_api_stats(conn, bucket_hour=hour, requests=75, cache_hits=45, status_2xx=72, status_4xx=3)
    stats = await get_api_stats(conn, limit=10)
    assert len(stats) == 1
    assert stats[0]["requests"] == 75  # upsert overwrites
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_uw.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement UW queries**

```python
# src/xenon/db/queries/uw.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import uw_api_stats, uw_flow_events, uw_analyze_snapshots


async def save_snapshot(
    conn: AsyncConnection,
    *,
    ticker: str,
    vrp_state: dict | None = None,
    regime: dict | None = None,
    flow_signals: dict | None = None,
    portfolio_score: Decimal | None = None,
) -> None:
    await conn.execute(
        insert(uw_analyze_snapshots).values(
            ticker=ticker,
            vrp_state=vrp_state,
            regime=regime,
            flow_signals=flow_signals,
            portfolio_score=portfolio_score,
        )
    )


async def get_latest_snapshot(
    conn: AsyncConnection, *, ticker: str
) -> dict | None:
    stmt = (
        select(uw_analyze_snapshots)
        .where(uw_analyze_snapshots.c.ticker == ticker)
        .order_by(uw_analyze_snapshots.c.snapshot_at.desc(), uw_analyze_snapshots.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def get_snapshot_history(
    conn: AsyncConnection, *, ticker: str, limit: int = 100
) -> list[dict]:
    stmt = (
        select(uw_analyze_snapshots)
        .where(uw_analyze_snapshots.c.ticker == ticker)
        .order_by(uw_analyze_snapshots.c.snapshot_at.desc())
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def save_flow_event(
    conn: AsyncConnection,
    *,
    ticker: str,
    side: str | None = None,
    strike: Decimal | None = None,
    expiry: date | None = None,
    detected_at: datetime,
    initial: dict,
    status: str = "open",
    daily_track: dict | None = None,
    anomaly_reason: str | None = None,
    closed_at: datetime | None = None,
) -> int:
    result = await conn.execute(
        insert(uw_flow_events)
        .values(
            ticker=ticker,
            side=side,
            strike=strike,
            expiry=expiry,
            detected_at=detected_at,
            initial=initial,
            status=status,
            daily_track=daily_track,
            anomaly_reason=anomaly_reason,
            closed_at=closed_at,
        )
        .returning(uw_flow_events.c.id)
    )
    return result.scalar()


async def get_flow_events(
    conn: AsyncConnection, *, status: str | None = None, ticker: str | None = None
) -> list[dict]:
    stmt = select(uw_flow_events).order_by(uw_flow_events.c.detected_at.desc())
    if status:
        stmt = stmt.where(uw_flow_events.c.status == status)
    if ticker:
        stmt = stmt.where(uw_flow_events.c.ticker == ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def upsert_api_stats(
    conn: AsyncConnection,
    *,
    bucket_hour: datetime,
    requests: int = 0,
    cache_hits: int = 0,
    latency_sum: Decimal = Decimal("0"),
    latency_count: int = 0,
    status_2xx: int = 0,
    status_4xx: int = 0,
    status_5xx: int = 0,
) -> None:
    values = dict(
        bucket_hour=bucket_hour,
        requests=requests,
        cache_hits=cache_hits,
        latency_sum=latency_sum,
        latency_count=latency_count,
        status_2xx=status_2xx,
        status_4xx=status_4xx,
        status_5xx=status_5xx,
    )
    stmt = pg_insert(uw_api_stats).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[uw_api_stats.c.bucket_hour],
        set_={k: stmt.excluded[k] for k in values if k != "bucket_hour"},
    )
    await conn.execute(stmt)


async def get_api_stats(
    conn: AsyncConnection, *, limit: int = 96
) -> list[dict]:
    stmt = (
        select(uw_api_stats)
        .order_by(uw_api_stats.c.bucket_hour.desc())
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_uw.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/uw.py src/xenon/db/tests/test_uw.py
git commit -m "feat(db): UW analysis query functions (snapshots, flow, API stats)"
```

---

## Task 12: Ticker Cache Queries

**Files:**

- Create: `src/xenon/db/queries/cache.py`
- Create: `src/xenon/db/tests/test_cache.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_cache.py
import pytest
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_set_and_get_cache(conn):
    from xenon.db.queries.cache import set_cached, get_cached

    await set_cached(conn, ticker="AAPL", cache_type="analyst_ratings", data={"buy": 15, "hold": 5, "sell": 1})
    result = await get_cached(conn, ticker="AAPL", cache_type="analyst_ratings")
    assert result["data"]["buy"] == 15


@pytest.mark.asyncio
async def test_cache_upsert(conn):
    from xenon.db.queries.cache import set_cached, get_cached

    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple"})
    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple Inc."})
    result = await get_cached(conn, ticker="AAPL", cache_type="company_info")
    assert result["data"]["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_expired_cache_returns_none(conn):
    from xenon.db.queries.cache import set_cached, get_cached

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple"}, expires_at=past)
    result = await get_cached(conn, ticker="AAPL", cache_type="company_info")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_cache.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement cache queries**

```python
# src/xenon/db/queries/cache.py
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import ticker_cache


async def set_cached(
    conn: AsyncConnection,
    *,
    ticker: str,
    cache_type: str,
    data: dict,
    expires_at: datetime | None = None,
) -> None:
    stmt = pg_insert(ticker_cache).values(
        ticker=ticker,
        cache_type=cache_type,
        data=data,
        expires_at=expires_at,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "cache_type"],
        set_={
            "data": stmt.excluded.data,
            "expires_at": stmt.excluded.expires_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await conn.execute(stmt)


async def get_cached(
    conn: AsyncConnection, *, ticker: str, cache_type: str
) -> dict | None:
    now = datetime.now(timezone.utc)
    stmt = select(ticker_cache).where(
        and_(
            ticker_cache.c.ticker == ticker,
            ticker_cache.c.cache_type == cache_type,
            # Not expired (NULL expires_at = never expires)
            (ticker_cache.c.expires_at.is_(None)) | (ticker_cache.c.expires_at > now),
        )
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def delete_expired(conn: AsyncConnection) -> int:
    from sqlalchemy import delete

    now = datetime.now(timezone.utc)
    result = await conn.execute(
        delete(ticker_cache).where(ticker_cache.c.expires_at <= now)
    )
    return result.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_cache.py -xvs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/cache.py src/xenon/db/tests/test_cache.py
git commit -m "feat(db): ticker cache query functions with TTL"
```

---

## Task 13: Event Bus (LISTEN/NOTIFY + Outbox)

**Files:**

- Create: `src/xenon/db/events.py`
- Create: `src/xenon/db/tests/test_events.py`

- [ ] **Step 1: Write failing tests**

```python
# src/xenon/db/tests/test_events.py
import pytest
import asyncio


@pytest.mark.asyncio
async def test_emit_inserts_into_outbox(conn):
    from xenon.db.events import emit
    from xenon.db.schema import outbox
    from sqlalchemy import select

    await emit(conn, channel="position.synced", source="xenon", payload={"account": "IB", "count": 5})
    result = await conn.execute(select(outbox))
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].channel == "position.synced"
    assert rows[0].payload["count"] == 5


@pytest.mark.asyncio
async def test_get_events_since(conn):
    from xenon.db.events import emit, get_events_since

    await emit(conn, channel="scan.completed", source="xenon", payload={"type": "gex"})
    await emit(conn, channel="scan.completed", source="xenon", payload={"type": "vcg"})
    events = await get_events_since(conn, channel="scan.completed", since_id=0)
    assert len(events) == 2
    events_after_first = await get_events_since(conn, channel="scan.completed", since_id=events[0]["id"])
    assert len(events_after_first) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_events.py -xvs`
Expected: FAIL

- [ ] **Step 3: Implement event bus**

```python
# src/xenon/db/events.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import asyncpg
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import outbox

logger = logging.getLogger(__name__)


async def emit(
    conn: AsyncConnection,
    *,
    channel: str,
    source: str,
    payload: dict,
) -> int:
    result = await conn.execute(
        insert(outbox)
        .values(channel=channel, source=source, payload=payload)
        .returning(outbox.c.id)
    )
    event_id = result.scalar()
    # pg_notify is handled by the outbox_notify_trigger — no manual call needed
    return event_id


async def get_events_since(
    conn: AsyncConnection,
    *,
    channel: str,
    since_id: int,
    limit: int = 100,
) -> list[dict]:
    stmt = (
        select(outbox)
        .where(outbox.c.channel == channel, outbox.c.id > since_id)
        .order_by(outbox.c.id)
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


class EventSubscriber:
    """Long-lived LISTEN subscriber using a raw asyncpg connection."""

    def __init__(self, dsn: str, channels: list[str]):
        self._dsn = dsn
        self._channels = channels
        self._conn: asyncpg.Connection | None = None
        self._callbacks: dict[str, list[Callable]] = {}
        self._last_seen: dict[str, int] = {ch: 0 for ch in channels}
        self._task: asyncio.Task | None = None

    def on(self, channel: str, callback: Callable) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        raw_dsn = self._dsn.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
        self._conn = await asyncpg.connect(raw_dsn)
        for ch in self._channels:
            await self._conn.add_listener(ch, self._on_notification)
        logger.info("EventSubscriber listening on %s", self._channels)

    def _on_notification(self, connection, pid, channel, payload):
        for cb in self._callbacks.get(channel, []):
            self._loop.call_soon_threadsafe(cb, channel, payload)

    async def stop(self) -> None:
        if self._conn:
            for ch in self._channels:
                await self._conn.remove_listener(ch, self._on_notification)
            await self._conn.close()
            self._conn = None
```

- [ ] **Step 4: Add outbox trigger migration**

Generate a new Alembic migration for the NOTIFY trigger:

```bash
uv run alembic revision -m "add outbox notify trigger"
```

In the generated file, add:

```python
def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION events.notify_outbox()
        RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify(NEW.channel, NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER outbox_notify_trigger
        AFTER INSERT ON events.outbox
        FOR EACH ROW EXECUTE FUNCTION events.notify_outbox();
    """)

def downgrade():
    op.execute("""
        DROP TRIGGER IF EXISTS outbox_notify_trigger ON events.outbox;
        DROP FUNCTION IF EXISTS events.notify_outbox();
    """)
```

Run: `uv run alembic upgrade head`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_events.py -xvs`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/events.py src/xenon/db/tests/test_events.py src/xenon/db/migrations/versions/
git commit -m "feat(db): event bus with LISTEN/NOTIFY + outbox trigger"
```

---

## Task 14: FastAPI Lifespan Integration

> **Execution order note:** After this task, run Task 15 (data migration) BEFORE Tasks 16-20 (read/write path replacement). The app reads from Postgres — the data must be there first.

**Files:**

- Modify: `src/xenon/api/server.py` (lifespan function, lines ~233-464)

- [ ] **Step 1: Write failing test for engine in app state**

```python
# src/xenon/db/tests/test_lifespan.py
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_engine_initialized_in_lifespan():
    from xenon.db.engine import get_engine

    # After lifespan runs, engine should be available
    # This test verifies the integration contract
    from xenon.db.engine import init_engine, dispose_engine

    engine = init_engine("postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test")
    assert get_engine() is engine
    await dispose_engine()
```

- [ ] **Step 2: Run test to verify it passes (engine module already supports this)**

Run: `uv run pytest src/xenon/db/tests/test_lifespan.py -xvs`
Expected: PASS

- [ ] **Step 3: Modify server.py lifespan**

In `src/xenon/api/server.py`, find the lifespan function. Add engine initialization at startup and disposal at shutdown.

At the top of the file, add import:

```python
from xenon.db.engine import init_engine, dispose_engine
```

In the startup section (after `orders_store.init_store()` or replacing it), add:

```python
# Initialize Postgres connection pool
db_engine = init_engine()
app.state.db_engine = db_engine
```

In the shutdown section (before final cleanup), add:

```python
# Dispose Postgres pool
await dispose_engine()
```

Keep `orders_store.init_store()` for now — it will be removed in Task 16.

- [ ] **Step 4: Verify server starts with Postgres**

Run: `uv run python -c "from xenon.api.server import app; print('OK')"` to verify import works.

Then start the dev server briefly to confirm no startup crash:

```bash
timeout 10 uv run uvicorn xenon.api.server:app --port 8321 || true
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py src/xenon/db/tests/test_lifespan.py
git commit -m "feat(db): wire Postgres engine into FastAPI lifespan"
```

---

## Task 16: Migrate ib_sync.py (Portfolio + NAV)

**Files:**

- Modify: `src/xenon/execution/ib_sync.py`

This task replaces `save_portfolio()` (atomic JSON) and `_append_nav_snapshot()` (JSONL) with Postgres writes.

- [ ] **Step 1: Identify current callers**

`save_portfolio()` is called from the IB sync flow. `_append_nav_snapshot()` is called within `save_portfolio()`. Both are sync functions called from a sync context (subprocess or sync IB callback).

Since these are sync functions but our DB layer is async, wrap with `asyncio.run()` or use the sync engine. Check how the existing code is invoked:

- If called from `asyncio.to_thread()` → can use a sync wrapper
- If called from a subprocess → needs its own engine

Read the calling context in `ib_sync.py` to determine the right approach.

- [ ] **Step 2: Modify save_portfolio()**

Replace the `atomic_save()` call with a Postgres write. Keep the function signature identical. The function needs to:

1. Delete existing positions for the account
2. Insert new positions
3. Insert account snapshot
4. Upsert NAV for today

Since `ib_sync.py` runs as a subprocess (not inside FastAPI), it needs its own engine. Add a helper:

```python
# At top of ib_sync.py
from xenon.db.engine import create_engine
from xenon.db.queries.portfolio import save_positions, save_account_snapshot, upsert_nav

_sync_engine = None

def _get_sync_engine():
    global _sync_engine
    if _sync_engine is None:
        import os
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL not set — no silent fallback to JSON files post-migration.")
        from sqlalchemy import create_engine as create_sync_engine
        sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        _sync_engine = create_sync_engine(sync_url)
    return _sync_engine
```

Require DATABASE_URL. If absent, raise RuntimeError — no silent fallback to JSON files post-migration.

- [ ] **Step 3: Modify \_append_nav_snapshot()**

Replace JSONL read/write with:

```python
def _append_nav_snapshot(net_liq, daily_pnl=None):
    engine = _get_sync_engine()
    if engine:
        from xenon.db.queries.portfolio import upsert_nav
        from datetime import date
        from decimal import Decimal
        with engine.begin() as conn:
            # Use sync version of upsert_nav
            conn.execute(...)  # Direct SQLAlchemy Core
    # ... existing JSONL fallback
```

- [ ] **Step 4: Test the modified sync path**

Write a test that uses the sync engine directly:

```bash
uv run pytest src/xenon/db/tests/test_portfolio.py -xvs
```

Also verify the subprocess flow still works:

```bash
uv run python -c "from xenon.execution.ib_sync import save_portfolio; print('import OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py
git commit -m "feat(db): migrate ib_sync portfolio + NAV writes to Postgres"
```

---

## Task 17: Migrate orders_store.py (DuckDB → Postgres)

**Files:**

- Modify: `src/xenon/execution/orders_store.py`
- Modify: `src/xenon/api/tests/conftest.py`

This is the highest-risk migration — the order lifecycle state machine. The strategy: keep the same public API, replace the DuckDB internals with calls to `xenon.db.queries.orders`.

- [ ] **Step 1: Audit all callers of orders_store**

Search for all imports of `orders_store` across the codebase to understand the full call surface:

```bash
grep -rn "orders_store" src/xenon/ --include="*.py" | grep -v __pycache__
```

Document every call site. Each must continue to work after the migration.

- [ ] **Step 2: Replace init_store()**

Replace DuckDB table creation with a no-op (Alembic handles schema). The function must still exist since `server.py` calls it.

```python
def init_store(db_path=None):
    """No-op — schema managed by Alembic. Kept for backward compatibility."""
    pass
```

- [ ] **Step 3: Replace each DuckDB function with Postgres equivalent**

Keep orders_store.py as the public facade. Preserve all existing function signatures, dataclasses (RequestRow, ReservationOutcome, SubmissionRow, WorkingReservations), and return types. Replace only the DuckDB internals (\_connect_utc, raw SQL) with calls to xenon.db.queries.orders using a sync psycopg engine. The \_WRITE_LOCK can be removed — Postgres handles row-level locking.

Key decision: since `orders_store.py` functions are called from both:

1. FastAPI async handlers (via `asyncio.to_thread()`)
2. Sync subprocesses (ib_place_order.py)

The safest approach is to keep them sync, using `sqlalchemy.create_engine` (sync) instead of async. This avoids event loop nesting issues.

- [ ] **Step 4: Update conftest.py**

In `src/xenon/api/tests/conftest.py`, replace `_isolate_orders_db()` fixture:

```python
@pytest.fixture(autouse=True)
def _isolate_orders_db(tmp_path, monkeypatch):
    # Point to test database instead of DuckDB temp dir
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL_TEST", "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test"))
```

- [ ] **Step 5: Run existing orders_store tests**

```bash
uv run pytest scripts/tests/ -k "order" -xvs
uv run pytest src/xenon/api/tests/ -k "order" -xvs
```

Expected: All existing order tests pass against Postgres.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/execution/orders_store.py src/xenon/api/tests/conftest.py
git commit -m "feat(db): migrate orders_store from DuckDB to Postgres"
```

---

## Task 18: Migrate ib_execute.py (Trade Log)

**Files:**

- Modify: `src/xenon/execution/ib_execute.py`

- [ ] **Step 1: Replace JSON trade log append**

In `OrderExecutor.log_trade()` (around line 274), replace the JSON file append with a Postgres INSERT.

The function is sync (called from IB callbacks). Use the sync engine pattern from Task 15.

Replace:

```python
# Old: load JSON, append, write back
with open(TRADE_LOG_PATH, "r") as f:
    data = json.load(f)
data["trades"].append(entry)
with open(TRADE_LOG_PATH, "w") as f:
    json.dump(data, f, indent=2)
```

With:

```python
# New: INSERT into xenon.trades
engine = _get_sync_engine()
if engine:
    with engine.begin() as conn:
        conn.execute(
            trades_table.insert().values(
                ticker=entry["ticker"],
                action=entry["action"],
                quantity=entry["quantity"],
                # ... map all fields
            )
        )
```

- [ ] **Step 2: Verify trade log still works**

```bash
uv run pytest scripts/tests/ -k "trade" -xvs
```

- [ ] **Step 3: Commit**

```bash
git add src/xenon/execution/ib_execute.py
git commit -m "feat(db): migrate trade log from JSON to Postgres"
```

---

## Task 19: Migrate UW Services

**Files:**

- Modify: `src/xenon/api/services/uw_analyze_cache.py`
- Modify: `src/xenon/api/services/uw_analyze_flow_tracker.py`
- Modify: `src/xenon/utils/uw_api_stats.py`

- [ ] **Step 1: Migrate uw_analyze_cache.py**

Replace `_persist()` (tmpfile + os.replace) with Postgres INSERT into `uw_analyze_snapshots`. Replace `_ensure_loaded()` (JSON file read) with SELECT from `uw_analyze_snapshots`.

Keep the in-memory OrderedDict cache — Postgres is the durable store, memory is the hot cache. On startup, load from Postgres instead of JSON file.

This runs in FastAPI's async context, so use the async engine:

```python
async def _persist(self):
    engine = get_engine()
    async with engine.begin() as conn:
        for ticker, entry in self._entries.items():
            await save_snapshot(conn, ticker=ticker, ...)
```

- [ ] **Step 2: Migrate uw_analyze_flow_tracker.py**

Replace `FlowLog.save()` (tmpfile + os.replace → `data/uw_unusual_flow_log.json`) with Postgres writes.
Replace `FlowLog.load()` with SELECT from `uw_flow_events`.

- [ ] **Step 3: Migrate uw_api_stats.py**

Replace `flush_history()` JSON write with Postgres upsert into `uw_api_stats`.
Replace `_load_history()` JSON read with SELECT.

Note: `uw_api_stats.py` uses `threading.Lock` because it's called from sync contexts. The Postgres writes here should use the sync engine.

- [ ] **Step 4: Run UW-related tests**

```bash
uv run pytest scripts/tests/ -k "uw" -xvs
uv run pytest src/xenon/api/tests/ -k "uw" -xvs
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/uw_analyze_cache.py src/xenon/api/services/uw_analyze_flow_tracker.py src/xenon/utils/uw_api_stats.py
git commit -m "feat(db): migrate UW services to Postgres"
```

---

## Task 20: Migrate Scanner Cache Writes

**Files:**

- Modify: scanner CLI entry points that write `data/scanner.json`, `data/discover.json`, `data/gex.json`, `data/vcg.json`, `data/cri.json`
- Modify: CRI scheduled scan writer

- [ ] **Step 1: Find all scanner write callsites**

```bash
grep -rn "write_json\|_write_cache\|json\.dump" src/xenon/scanners/ --include="*.py" | grep -v __pycache__
grep -rn "data/scanner\|data/discover\|data/gex\|data/vcg\|data/cri" src/xenon/ --include="*.py" | grep -v __pycache__
```

- [ ] **Step 2: Replace each write with save_scan()**

For each scanner that writes a JSON file, replace with:

```python
engine = _get_sync_engine()
if engine:
    with engine.begin() as conn:
        conn.execute(
            scan_results_table.insert().values(
                scan_type="watchlist",  # or discover, gex, vcg, cri
                payload=result_dict,
            )
        )
```

For CRI scheduled scans, use `save_cri_datapoint()`.

- [ ] **Step 3: Replace each read with get_latest_scan()**

Find all places that read scanner JSON files (typically in FastAPI routes or services) and replace with `get_latest_scan(conn, scan_type="...")`.

- [ ] **Step 4: Run scanner tests**

```bash
uv run pytest scripts/tests/ -k "scan" -xvs
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/scanners/ src/xenon/api/
git commit -m "feat(db): migrate scanner results to Postgres"
```

---

## Task 15: One-Time Data Migration Script (RUN BEFORE TASKS 16-21)

> **CRITICAL ORDERING:** This task MUST run before any read/write path replacement (Tasks 16-21). The app will read from Postgres after cutover — the data must be there first.

**Files:**

- Create: `scripts/migrations/migrate_to_postgres.py`

- [ ] **Step 1: Implement migration script**

```python
# scripts/migrations/migrate_to_postgres.py
"""One-time migration: JSON/DuckDB → PostgreSQL.

Usage:
    uv run python scripts/migrations/migrate_to_postgres.py

Requires DATABASE_URL env var and existing data/ directory.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
from sqlalchemy import create_engine, text

DATA_DIR = Path("data")

def get_engine():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return create_engine(url)


def migrate_portfolio(engine):
    path = DATA_DIR / "portfolio.json"
    if not path.exists():
        print("  SKIP portfolio.json (not found)")
        return 0
    with open(path) as f:
        data = json.load(f)
    data.pop("_checksum", None)

    count = 0
    with engine.begin() as conn:
        # Account snapshot
        conn.execute(text("""
            INSERT INTO xenon.account_snapshots (account, bankroll, peak_value, net_liquidation)
            VALUES (:account, :bankroll, :peak_value, :net_liq)
        """), {
            "account": "IB",
            "bankroll": data.get("bankroll", 0),
            "peak_value": data.get("peak_value"),
            "net_liq": data.get("net_liquidation"),
        })

        # Positions
        for pos in data.get("positions", []):
            conn.execute(text("""
                INSERT INTO xenon.positions (ticker, security_type, expiry, strike, "right", quantity, avg_cost, current_price, unrealized_pnl, account)
                VALUES (:ticker, :sec_type, :expiry, :strike, :right, :qty, :avg_cost, :cur_price, :pnl, :account)
            """), {
                "ticker": pos.get("ticker", pos.get("symbol", "")),
                "sec_type": pos.get("security_type", pos.get("secType", "STK")),
                "expiry": pos.get("expiry"),
                "strike": pos.get("strike"),
                "right": pos.get("right"),
                "qty": pos.get("quantity", pos.get("position", 0)),
                "avg_cost": pos.get("avg_cost", pos.get("avgCost", 0)),
                "cur_price": pos.get("current_price", pos.get("marketPrice")),
                "pnl": pos.get("unrealized_pnl", pos.get("unrealizedPNL")),
                "account": "IB",
            })
            count += 1
    print(f"  portfolio.json → {count} positions + 1 snapshot")
    return count


def migrate_nav_history(engine):
    path = DATA_DIR / "nav_history.jsonl"
    if not path.exists():
        print("  SKIP nav_history.jsonl (not found)")
        return 0
    count = 0
    with engine.begin() as conn:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            conn.execute(text("""
                INSERT INTO xenon.nav_history (date, nav, daily_pnl)
                VALUES (:date, :nav, :pnl)
                ON CONFLICT (date) DO UPDATE SET nav = :nav, daily_pnl = :pnl
            """), {
                "date": entry["date"],
                "nav": entry["nav"],
                "pnl": entry.get("daily_pnl"),
            })
            count += 1
    print(f"  nav_history.jsonl → {count} rows")
    return count


def migrate_trade_log(engine):
    path = DATA_DIR / "trade_log.json"
    if not path.exists():
        print("  SKIP trade_log.json (not found)")
        return 0
    with open(path) as f:
        data = json.load(f)
    trades = data.get("trades", [])
    count = 0
    with engine.begin() as conn:
        for t in trades:
            conn.execute(text("""
                INSERT INTO xenon.trades (ticker, structure, action, quantity, entry_cost, exit_cost, realized_pnl, edge, decision, metadata)
                VALUES (:ticker, :structure, :action, :quantity, :entry_cost, :exit_cost, :pnl, :edge, :decision, :meta::jsonb)
            """), {
                "ticker": t.get("ticker", ""),
                "structure": t.get("structure"),
                "action": t.get("action", t.get("side", "")),
                "quantity": t.get("quantity", 0),
                "entry_cost": t.get("entry_cost"),
                "exit_cost": t.get("exit_cost"),
                "pnl": t.get("realized_pnl", t.get("pnl")),
                "edge": t.get("edge"),
                "decision": t.get("decision"),
                "meta": json.dumps(t),
            })
            count += 1
    print(f"  trade_log.json → {count} trades")
    return count


def migrate_orders_duckdb(engine):
    db_path = DATA_DIR / "orders.duckdb"
    if not db_path.exists():
        print("  SKIP orders.duckdb (not found)")
        return 0

    duck = duckdb.connect(str(db_path), read_only=True)
    duck.execute("SET TimeZone='UTC'")

    count_sub = 0
    count_evt = 0
    count_ws = 0
    count_we = 0

    with engine.begin() as conn:
        # Order submissions
        rows = duck.execute("SELECT * FROM orders_submissions").fetchall()
        cols = [desc[0] for desc in duck.description]
        for row in rows:
            d = dict(zip(cols, row))
            conn.execute(text("""
                INSERT INTO xenon.order_submissions (submission_id, user_id, client_attempt_id, ticker, security_type, action, quantity, expiry, strike, "right", multiplier, con_id, placing_client_id, ib_order_id, perm_id, limit_price, state, reason_code, filled_qty, avg_fill_price, modify_sequence, submitted_at, updated_at)
                VALUES (:submission_id, :user_id, :client_attempt_id, :ticker, :security_type, :action, :quantity, :expiry, :strike, :right, :multiplier, :con_id, :placing_client_id, :ib_order_id, :perm_id, :limit_price, :state, :reason_code, :filled_qty, :avg_fill_price, :modify_sequence, :submitted_at, :updated_at)
                ON CONFLICT (submission_id) DO NOTHING
            """), d)
            count_sub += 1

        # Order events
        rows = duck.execute("SELECT * FROM orders_events").fetchall()
        cols = [desc[0] for desc in duck.description]
        for row in rows:
            d = dict(zip(cols, row))
            if isinstance(d.get("detail"), str):
                d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            conn.execute(text("""
                INSERT INTO xenon.order_events (submission_id, kind, detail, "at")
                VALUES (:submission_id, :kind, :detail::jsonb, :at)
            """), {
                "submission_id": d["submission_id"],
                "kind": d["kind"],
                "detail": json.dumps(d["detail"]) if d["detail"] else None,
                "at": d["at"],
            })
            count_evt += 1

        # Wizard sessions
        try:
            rows = duck.execute("SELECT * FROM wizard_sessions").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("payload"), str):
                    d["payload"] = json.loads(d["payload"]) if d["payload"] else None
                conn.execute(text("""
                    INSERT INTO xenon.wizard_sessions (session_id, ticker, state, structure_name, intent, payload, current_attempt_id, created_at, updated_at)
                    VALUES (:session_id, :ticker, :state, :structure_name, :intent, :payload::jsonb, :current_attempt_id, :created_at, :updated_at)
                    ON CONFLICT (session_id) DO NOTHING
                """), {
                    "session_id": d["session_id"],
                    "ticker": d["ticker"],
                    "state": d["state"],
                    "structure_name": d.get("structure_name"),
                    "intent": d.get("intent"),
                    "payload": json.dumps(d["payload"]) if d.get("payload") else None,
                    "current_attempt_id": d.get("current_attempt_id"),
                    "created_at": d.get("created_at"),
                    "updated_at": d.get("updated_at"),
                })
                count_ws += 1
        except duckdb.CatalogException:
            print("  SKIP wizard_sessions (table not found)")

        # Wizard events (DuckDB table is named wizard_session_events)
        try:
            rows = duck.execute("SELECT * FROM wizard_session_events").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("detail"), str):
                    d["detail"] = json.loads(d["detail"]) if d["detail"] else None
                conn.execute(text("""
                    INSERT INTO xenon.wizard_events (session_id, kind, detail, "at")
                    VALUES (:session_id, :kind, :detail::jsonb, :at)
                """), {
                    "session_id": d["session_id"],
                    "kind": d["kind"],
                    "detail": json.dumps(d["detail"]) if d["detail"] else None,
                    "at": d["at"],
                })
                count_we += 1
        except duckdb.CatalogException:
            print("  SKIP wizard_session_events (table not found)")

        # Wizard combo attempts
        count_wca = 0
        try:
            rows = duck.execute("SELECT * FROM wizard_combo_attempts").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                for json_col in ("legs", "combo_contract"):
                    if isinstance(d.get(json_col), str):
                        d[json_col] = json.loads(d[json_col]) if d[json_col] else None
                conn.execute(text("""
                    INSERT INTO xenon.wizard_combo_attempts (attempt_id, session_id, ticker, structure_name, legs, combo_contract, ib_order_id, perm_id, placing_client_id, limit_price, state, reason_code, filled_qty, avg_fill_price, modify_sequence, submitted_at, updated_at)
                    VALUES (:attempt_id, :session_id, :ticker, :structure_name, :legs::jsonb, :combo_contract::jsonb, :ib_order_id, :perm_id, :placing_client_id, :limit_price, :state, :reason_code, :filled_qty, :avg_fill_price, :modify_sequence, :submitted_at, :updated_at)
                    ON CONFLICT (attempt_id) DO NOTHING
                """), {
                    "attempt_id": d["attempt_id"],
                    "session_id": d["session_id"],
                    "ticker": d.get("ticker", ""),
                    "structure_name": d.get("structure_name"),
                    "legs": json.dumps(d.get("legs")) if d.get("legs") else None,
                    "combo_contract": json.dumps(d.get("combo_contract")) if d.get("combo_contract") else None,
                    "ib_order_id": d.get("ib_order_id"),
                    "perm_id": d.get("perm_id"),
                    "placing_client_id": d.get("placing_client_id"),
                    "limit_price": d.get("limit_price"),
                    "state": d.get("state", ""),
                    "reason_code": d.get("reason_code"),
                    "filled_qty": d.get("filled_qty", 0),
                    "avg_fill_price": d.get("avg_fill_price"),
                    "modify_sequence": d.get("modify_sequence", 0),
                    "submitted_at": d.get("submitted_at"),
                    "updated_at": d.get("updated_at"),
                })
                count_wca += 1
        except duckdb.CatalogException:
            print("  SKIP wizard_combo_attempts (table not found)")

        # Wizard protection
        count_wp = 0
        try:
            rows = duck.execute("SELECT * FROM wizard_protection").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("config"), str):
                    d["config"] = json.loads(d["config"]) if d["config"] else {}
                conn.execute(text("""
                    INSERT INTO xenon.wizard_protection (session_id, attempt_id, protection_type, config, state, triggered_at, created_at)
                    VALUES (:session_id, :attempt_id, :protection_type, :config::jsonb, :state, :triggered_at, :created_at)
                """), {
                    "session_id": d["session_id"],
                    "attempt_id": d.get("attempt_id"),
                    "protection_type": d.get("protection_type", ""),
                    "config": json.dumps(d.get("config", {})),
                    "state": d.get("state", "active"),
                    "triggered_at": d.get("triggered_at"),
                    "created_at": d.get("created_at"),
                })
                count_wp += 1
        except duckdb.CatalogException:
            print("  SKIP wizard_protection (table not found)")

    duck.close()
    print(f"  orders.duckdb → {count_sub} submissions, {count_evt} events, {count_ws} wizard sessions, {count_we} wizard events, {count_wca} combo attempts, {count_wp} protections")
    return count_sub + count_evt


def migrate_scan_results(engine):
    count = 0
    with engine.begin() as conn:
        for scan_type, filename in [
            ("watchlist", "scanner.json"),
            ("discover", "discover.json"),
            ("gex", "gex.json"),
            ("vcg", "vcg.json"),
            ("cri", "cri.json"),
        ]:
            path = DATA_DIR / filename
            if not path.exists():
                continue
            with open(path) as f:
                data = json.load(f)
            conn.execute(text("""
                INSERT INTO xenon.scan_results (scan_type, payload)
                VALUES (:scan_type, :payload::jsonb)
            """), {"scan_type": scan_type, "payload": json.dumps(data)})
            count += 1

        # CRI series
        cri_dir = DATA_DIR / "cri_scheduled"
        if cri_dir.exists():
            for f in sorted(cri_dir.glob("*.json")):
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(text("""
                    INSERT INTO xenon.cri_series (cri_level, alert, payload)
                    VALUES (:level, :alert, :payload::jsonb)
                """), {
                    "level": data.get("cri_level", data.get("cri", 0)),
                    "alert": data.get("alert", False),
                    "payload": json.dumps(data),
                })
                count += 1

    print(f"  scan results → {count} rows")
    return count


def migrate_uw_data(engine):
    count = 0
    with engine.begin() as conn:
        # UW analyze cache
        path = DATA_DIR / "uw_analyze_cache.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            data.pop("_checksum", None)
            for ticker, entry in data.items():
                if ticker.startswith("_"):
                    continue
                conn.execute(text("""
                    INSERT INTO xenon.uw_analyze_snapshots (ticker, vrp_state, regime, flow_signals, portfolio_score)
                    VALUES (:ticker, :vrp::jsonb, :regime::jsonb, :flow::jsonb, :score)
                """), {
                    "ticker": ticker,
                    "vrp": json.dumps(entry.get("vrp_state")),
                    "regime": json.dumps(entry.get("regime")),
                    "flow": json.dumps(entry.get("flow_signals")),
                    "score": entry.get("portfolio_score"),
                })
                count += 1

        # UW flow events
        path = DATA_DIR / "uw_unusual_flow_log.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            for evt in data.get("events", []):
                conn.execute(text("""
                    INSERT INTO xenon.uw_flow_events (ticker, side, strike, expiry, detected_at, initial, daily_track, status, anomaly_reason, closed_at)
                    VALUES (:ticker, :side, :strike, :expiry, :detected_at, :initial::jsonb, :track::jsonb, :status, :reason, :closed_at)
                """), {
                    "ticker": evt.get("ticker", ""),
                    "side": evt.get("side"),
                    "strike": evt.get("strike"),
                    "expiry": evt.get("expiry"),
                    "detected_at": evt.get("detected_at"),
                    "initial": json.dumps(evt.get("initial", {})),
                    "track": json.dumps(evt.get("daily_track")),
                    "status": evt.get("status", "open"),
                    "reason": evt.get("anomaly_reason"),
                    "closed_at": evt.get("closed_at"),
                })
                count += 1

        # UW API stats
        path = DATA_DIR / "uw_api_stats_history.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            for bucket in data.get("hourly_buckets", []):
                conn.execute(text("""
                    INSERT INTO xenon.uw_api_stats (bucket_hour, requests, cache_hits, latency_sum, latency_count, status_2xx, status_4xx, status_5xx)
                    VALUES (:hour, :req, :cache, :lat_sum, :lat_count, :s2xx, :s4xx, :s5xx)
                    ON CONFLICT (bucket_hour) DO NOTHING
                """), {
                    "hour": bucket.get("timestamp"),
                    "req": bucket.get("requests", 0),
                    "cache": bucket.get("cache_hits", 0),
                    "lat_sum": bucket.get("latency_sum", 0),
                    "lat_count": bucket.get("latency_count", 0),
                    "s2xx": bucket.get("status_2xx", 0),
                    "s4xx": bucket.get("status_4xx", 0),
                    "s5xx": bucket.get("status_5xx", 0),
                })
                count += 1

    print(f"  UW data → {count} rows")
    return count


def migrate_caches(engine):
    count = 0
    with engine.begin() as conn:
        # Analyst ratings
        path = DATA_DIR / "analyst_ratings_cache.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            for ticker, ratings in data.items():
                conn.execute(text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'analyst_ratings', :data::jsonb, now())
                    ON CONFLICT (ticker, cache_type) DO UPDATE SET data = :data::jsonb, updated_at = now()
                """), {"ticker": ticker, "data": json.dumps(ratings)})
                count += 1

        # Company info
        cache_dir = DATA_DIR / "company_info_cache"
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                ticker = f.stem
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'company_info', :data::jsonb, now())
                    ON CONFLICT (ticker, cache_type) DO UPDATE SET data = :data::jsonb, updated_at = now()
                """), {"ticker": ticker, "data": json.dumps(data)})
                count += 1

        # Seasonality
        cache_dir = DATA_DIR / "seasonality_cache"
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                ticker = f.stem
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'seasonality', :data::jsonb, now())
                    ON CONFLICT (ticker, cache_type) DO UPDATE SET data = :data::jsonb, updated_at = now()
                """), {"ticker": ticker, "data": json.dumps(data)})
                count += 1

    print(f"  caches → {count} rows")
    return count


def verify(engine):
    with engine.connect() as conn:
        tables = [
            "xenon.positions", "xenon.account_snapshots", "xenon.trades",
            "xenon.nav_history", "xenon.order_submissions", "xenon.order_events",
            "xenon.wizard_sessions", "xenon.wizard_events",
            "xenon.wizard_combo_attempts", "xenon.wizard_protection",
            "xenon.scan_results", "xenon.cri_series",
            "xenon.uw_analyze_snapshots", "xenon.uw_flow_events", "xenon.uw_api_stats",
            "xenon.ticker_cache",
        ]
        print("\n--- Verification ---")
        for table in tables:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = result.scalar()
            print(f"  {table}: {count} rows")


def main():
    print("=== Xenon Postgres Migration ===\n")

    engine = get_engine()

    print("Phase 1: Schema (handled by alembic upgrade head)\n")

    print("Phase 2: Critical data")
    migrate_portfolio(engine)
    migrate_nav_history(engine)
    migrate_trade_log(engine)
    migrate_orders_duckdb(engine)

    print("\nPhase 3: Scanner data")
    migrate_scan_results(engine)

    print("\nPhase 4: UW data")
    migrate_uw_data(engine)

    print("\nPhase 5: Caches")
    migrate_caches(engine)

    print("\nPhase 6: Verify")
    verify(engine)

    print("\n=== Migration complete ===")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test migration script on test database**

```bash
DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test uv run python scripts/migrations/migrate_to_postgres.py
```

Expected: All phases complete, verification shows row counts matching source data.

- [ ] **Step 3: Run migration on prod database**

```bash
uv run python scripts/migrations/migrate_to_postgres.py
```

- [ ] **Step 4: Commit**

```bash
git add scripts/migrations/migrate_to_postgres.py
git commit -m "feat(db): one-time data migration script (JSON/DuckDB → Postgres)"
```

---

## Task 21: Migrate Combo Wizard (DuckDB → Postgres)

**Files:**

- Create: `src/xenon/db/queries/combo_wizard.py`
- Create: `src/xenon/db/tests/test_combo_wizard.py`
- Modify: `src/xenon/execution/combo_wizard/store.py`
- Modify: `src/xenon/execution/combo_wizard/session.py`
- Modify: `src/xenon/execution/combo_wizard/ib_adapter.py`
- Modify: `src/xenon/execution/combo_wizard/rehydrate.py`
- Modify: `src/xenon/execution/combo_wizard/protect.py`
- Modify: `src/xenon/execution/combo_wizard/combo_quote_source.py`
- Modify: `src/xenon/execution/wizard_stop_monitor.py`
- Modify: `src/xenon/execution/single_leg_rehydrate.py`

These files all import `orders_store._connect_utc()` and execute raw DuckDB SQL against `wizard_sessions`, `wizard_session_events`, `wizard_combo_attempts`, and `wizard_protection` tables. They must be migrated to use `db.queries.combo_wizard` and `db.queries.wizard`.

- [ ] **Step 1: Audit all DuckDB call sites in combo_wizard/**

```bash
grep -rn "_connect_utc\|duckdb\|\.execute(" src/xenon/execution/combo_wizard/ --include="*.py" | grep -v __pycache__
grep -rn "_connect_utc\|duckdb\|\.execute(" src/xenon/execution/single_leg_rehydrate.py
grep -rn "_connect_utc\|duckdb\|\.execute(" src/xenon/execution/wizard_stop_monitor.py
```

Document every raw SQL query. Each must be converted to a query function.

- [ ] **Step 2: Write failing tests for combo_wizard query functions**

Write tests in `src/xenon/db/tests/test_combo_wizard.py` covering:

- `create_attempt()`, `get_attempt()`, `update_attempt_state()`
- `create_protection()`, `get_protections_for_session()`
- `get_attempts_for_session()`

Follow the same pattern as `test_orders.py` — use the `conn` fixture, assert on returned dicts.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest src/xenon/db/tests/test_combo_wizard.py -xvs`
Expected: FAIL

- [ ] **Step 4: Implement combo_wizard query functions**

Create `src/xenon/db/queries/combo_wizard.py` with functions matching the DuckDB SQL patterns found in Step 1. Key functions:

- `create_attempt(conn, *, attempt_id, session_id, ticker, structure_name, legs, ...)` → INSERT
- `get_attempt(conn, attempt_id)` → SELECT
- `update_attempt_state(conn, *, attempt_id, state, ib_order_id, perm_id, ...)` → UPDATE
- `mark_attempt_terminal(conn, *, attempt_id, state, filled_qty, avg_fill_price)` → UPDATE
- `apply_attempt_modify(conn, *, attempt_id, modify_sequence)` → compare-and-swap UPDATE
- `create_protection(conn, *, session_id, attempt_id, protection_type, config)` → INSERT
- `get_protections_for_session(conn, session_id)` → SELECT
- `update_protection_state(conn, *, protection_id, state)` → UPDATE

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest src/xenon/db/tests/test_combo_wizard.py -xvs`
Expected: PASS

- [ ] **Step 6: Migrate combo_wizard/store.py**

Replace `_init_tables()` (DuckDB CREATE TABLE) with a no-op — Alembic manages schema.
Replace all `_connect_utc()` + raw SQL with calls to `db.queries.combo_wizard` using a sync psycopg engine (same `_get_sync_engine()` pattern from Task 16).

- [ ] **Step 7: Migrate remaining combo_wizard modules**

For each of `session.py`, `ib_adapter.py`, `rehydrate.py`, `protect.py`, `combo_quote_source.py`:

- Replace `from xenon.execution.orders_store import _connect_utc, _resolve_path` with sync engine
- Replace all raw DuckDB SQL with query function calls
- Preserve function signatures and return types

For `single_leg_rehydrate.py` and `wizard_stop_monitor.py`:

- Same pattern — replace DuckDB reads with Postgres query calls

- [ ] **Step 8: Run existing combo wizard tests**

```bash
uv run pytest scripts/tests/ -k "wizard or combo" -xvs
uv run pytest src/xenon/api/tests/ -k "wizard or combo" -xvs
```

Expected: All existing tests pass against Postgres.

- [ ] **Step 9: Commit**

```bash
git add src/xenon/db/queries/combo_wizard.py src/xenon/db/tests/test_combo_wizard.py src/xenon/execution/combo_wizard/ src/xenon/execution/single_leg_rehydrate.py src/xenon/execution/wizard_stop_monitor.py
git commit -m "feat(db): migrate combo wizard from DuckDB to Postgres"
```

---

## Task 22: Cleanup

**Files:**

- Modify: `pyproject.toml` (remove duckdb)
- Delete: dead JSON read/write code paths
- Modify: `src/xenon/api/server.py` (remove orders_store.init_store call)

- [ ] **Step 1: Remove DuckDB dependency**

In `pyproject.toml`, remove:

```
"duckdb>=1.2.0",
```

Run: `uv sync --extra test`

- [ ] **Step 2: Remove dead code**

Remove or stub out:

- `src/xenon/execution/orders_store.py` DuckDB internals (keep as thin wrapper calling `xenon.db.queries.orders`)
- `atomic_save()` calls in `ib_sync.py` (now writes to Postgres)
- JSON file reads in `uw_analyze_cache._ensure_loaded()` (now reads from Postgres)
- JSON file writes in `uw_api_stats.flush_history()` (now writes to Postgres)
- JSON file writes in `uw_analyze_flow_tracker.FlowLog.save()` (now writes to Postgres)

Do NOT delete the `data/` directory — it's the backup.

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest -x
cd web && npm test
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock src/
git commit -m "chore(db): remove DuckDB dependency, clean up dead JSON persistence code"
```

---

## Task 23: Update CLAUDE.md + Documentation

**Files:**

- Modify: `CLAUDE.md`
- Modify: `src/xenon/CLAUDE.md`

- [ ] **Step 1: Update root CLAUDE.md**

Add to Credentials table:

```
| `.env` (root) | `python-dotenv` | ... `DATABASE_URL` |
```

Add to Startup Checklist:

```
- [ ] `psql -h localhost -U xenon_app xenon_db -c "SELECT 1"` — verify Postgres accessible
```

- [ ] **Step 2: Update src/xenon/CLAUDE.md**

Add a Database section documenting:

- `src/xenon/db/` module structure
- How to add a new table (schema.py → alembic revision → query module)
- How to run migrations (`uv run alembic upgrade head`)
- Event bus usage (emit + subscribe patterns)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md src/xenon/CLAUDE.md
git commit -m "docs: update CLAUDE.md for Postgres migration"
```
