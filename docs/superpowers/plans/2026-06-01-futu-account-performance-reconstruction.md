# Futu Account Performance Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest the full Futu trading history from 2024-05-01 forward into Postgres, reconstruct a cost-basis equity curve via backward-walk from today's anchor, expose it through FastAPI + a new Futu-tab UI, and add a cross-broker equity curve that overlays IB and Futu.

**Architecture:** All inputs come from Futu OpenD (`OpenSecTradeContext` for trades/cash flow/accinfo, `OpenQuoteContext.get_rehab` for corporate actions) plus FRED H.10 daily CSV for FX. New tables (`futu_trades`, `futu_cash_flow`, `corporate_actions`, `futu_reconciliation_log`, `fx_daily`) and a `source` column on `nav_history`. One idempotent `futu_history_sync` orchestrator drives both first-run backfill (backward-walk NAV reconstruction anchored at `cost_basis_NAV_today`) and daily-forward UPSERT of today's reported NAV. Six new FastAPI routes + four React components + one cross-broker chart.

**Tech Stack:** Python 3.13 + `uv`, FastAPI, SQLAlchemy + Alembic, Postgres 15+, `futu-api>=9.0`, `httpx`, Vitest + Next.js 15, Playwright. **No IB changes** — IB Gateway, `ib_async`, and IB historical-data endpoints are explicitly out of scope.

**Spec reference:** `docs/superpowers/specs/2026-06-01-futu-account-performance-reconstruction-design.md` (read this first; the plan reuses its terminology verbatim).

**Standing rules in force:** No commits without explicit user request (xenon CLAUDE.md). Always open a PR before merging to master (global CLAUDE.md). `uv` exclusively — never bare `python` / `pip` / `pytest`. No Yahoo Finance. No naked shorts (not relevant here — Futu is read-only). Never commit `.env`.

---

## Pre-flight

Before Task 1: confirm you are on a feature branch off `master`, and that `core_test` is reachable at `192.168.50.47:5432`.

```bash
git checkout -b feat/futu-history-reconstruction master
psql -h 192.168.50.47 -U xenon_app -d core_test -c "SELECT 1" </dev/null
```

`DATABASE_URL_TEST` is already wired in `.env`; pytest reads it. If the second command fails, fix the credentials/network before proceeding — the rest of the plan assumes a reachable test DB.

---

## Milestone 1 — Foundation (Tasks 1-4)

### Task 1: Alembic migration for Futu history tables

**Files:**

- Create: `src/xenon/db/migrations/versions/2026_06_01_0001_futu_history_tables.py`
- Modify: `src/xenon/db/schema.py` (append new `Table` definitions; alter `nav_history`)
- Modify: `src/xenon/api/services/advisory_lock.py` (add `LOCK_KEY_FUTU_HISTORY_SYNC`)
- Test: `scripts/tests/test_futu_migration.py`

- [ ] **Step 1: Write the migration smoke test**

```python
# scripts/tests/test_futu_migration.py
"""Verifies the Futu-history migration creates all expected tables/columns/views."""
import os
import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture(scope="module")
def engine():
    url = os.environ["DATABASE_URL_TEST"].replace("+asyncpg", "")
    return create_engine(url, future=True)


def test_futu_trades_table_exists(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("futu_trades", schema="xenon")}
    assert {
        "id", "futu_deal_id", "futu_order_id", "ticker", "futu_code", "market",
        "action", "quantity", "price", "fees", "realized_pnl", "filled_at",
        "raw", "broker", "account_env", "broker_account",
    } <= cols


def test_futu_cash_flow_table_exists(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("futu_cash_flow", schema="xenon")}
    assert {"futu_flow_id", "flow_type", "futu_type_raw", "amount",
            "amount_original", "original_currency", "fx_rate_used", "ticker",
            "description", "flow_at", "raw"} <= cols


def test_corporate_actions_table_exists(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("corporate_actions", schema="xenon")}
    assert {"ticker", "action_type", "effective_date",
            "ratio_numerator", "ratio_denominator", "new_ticker"} <= cols


def test_futu_reconciliation_log_table_exists(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("futu_reconciliation_log", schema="xenon")}
    assert {"kind", "recon_date", "reported_nav", "reconstructed_nav",
            "delta", "threshold", "exceeded", "notes"} <= cols


def test_fx_daily_table_exists(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("fx_daily", schema="xenon")}
    assert {"currency", "date", "currency_per_usd", "source"} <= cols


def test_nav_history_has_source_column(engine):
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("nav_history", schema="xenon")}
    assert "source" in cols, "nav_history.source column missing"
    # NOT NULL with default 'reported'
    assert cols["source"]["nullable"] is False


def test_all_fills_view_exists(engine):
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT count(*) FROM pg_views "
            "WHERE schemaname='xenon' AND viewname='all_fills'"
        )).scalar()
    assert result == 1


def test_futu_trades_ck_market_us_only(engine):
    """The check constraint must reject non-US markets."""
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError, match="ck_futu_trades_market_us"):
        with engine.begin() as conn:   # auto-rollback on exception
            conn.execute(text(
                "INSERT INTO xenon.futu_trades "
                "(futu_deal_id, futu_order_id, ticker, futu_code, market, action, "
                " quantity, price, filled_at, raw, account_env, broker_account) "
                "VALUES ('x','y','TENCENT','HK.00700','HK','BUY',100,300,now(),"
                "'{}'::jsonb,'live','test_acct')"
            ))
```

- [ ] **Step 2: Run test to confirm failure**

```bash
cd /Users/chenxi/projects/xenon
uv run pytest scripts/tests/test_futu_migration.py -xvs
```

Expected: `NoSuchTableError` or `relation "xenon.futu_trades" does not exist`.

- [ ] **Step 3: Add `LOCK_KEY_FUTU_HISTORY_SYNC` to advisory_lock.py**

Edit `src/xenon/api/services/advisory_lock.py` — find the existing `LOCK_KEY_VCG_CRI = 7342001` line and add directly below:

```python
LOCK_KEY_FUTU_HISTORY_SYNC = 7343001
```

- [ ] **Step 4: Add Table definitions to schema.py**

`src/xenon/db/schema.py` already imports the SQLAlchemy primitives and defines `xenon_metadata` at line 27. Do NOT re-add the imports.

(a) Extend the existing `nav_history` Table block (line 174-194) — add a `source` column and CheckConstraint. Find this section:

```python
nav_history = Table(
    "nav_history",
    xenon_metadata,
    Column("broker", Text, primary_key=True, server_default=text("'IB'")),
    ...
    Column("options_value", Numeric(14, 2)),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_nav_broker"),
    ...
)
```

Insert after `Column("options_value", Numeric(14, 2)),` and before the existing CheckConstraints:

```python
    Column("source", Text, nullable=False, server_default=text("'reported'")),
    CheckConstraint("source IN ('reported','reconstructed')",
                    name="ck_nav_history_source"),
```

(b) Append the new Tables after the existing `nav_history` block. Use `xenon_metadata` everywhere (NOT `metadata`).

futu_trades = Table(
"futu_trades", xenon_metadata,
Column("id", BigInteger, primary_key=True),
Column("futu_deal_id", Text, nullable=False),
Column("futu_order_id", Text, nullable=False),
Column("ticker", Text, nullable=False),
Column("futu_code", Text, nullable=False),
Column("market", Text, nullable=False),
Column("action", Text, nullable=False),
Column("quantity", Integer, nullable=False),
Column("price", Numeric(12, 4), nullable=False),
Column("fees", Numeric(10, 4), nullable=False, server_default=text("0")),
Column("realized_pnl", Numeric(12, 2)),
Column("filled_at", DateTime(timezone=True), nullable=False),
Column("raw", JSONB, nullable=False),
Column("broker", Text, nullable=False, server_default=text("'FUTU'")),
Column("account_env", Text, nullable=False),
Column("broker_account", Text, nullable=False),
CheckConstraint("broker = 'FUTU'", name="ck_futu_trades_broker"),
CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')",
name="ck_futu_trades_env"),
CheckConstraint("market = 'US'", name="ck_futu_trades_market_us"),
UniqueConstraint("broker", "account_env", "broker_account", "futu_deal_id",
name="uq_futu_trades_scope_deal"),
Index("ix_futu_trades_scope_filled", "broker", "account_env",
"broker_account", "filled_at"),
Index("ix_futu_trades_ticker_filled", "ticker", "filled_at"),
schema="xenon",
)

futu_cash_flow = Table(
"futu_cash_flow", xenon_metadata,
Column("id", BigInteger, primary_key=True),
Column("futu_flow_id", Text, nullable=False),
Column("flow_type", Text, nullable=False),
Column("futu_type_raw", Text, nullable=False),
Column("amount", Numeric(12, 2)), # NULLABLE on purpose — missing-FX path
Column("amount_original", Numeric(12, 2), nullable=False),
Column("original_currency", Text, nullable=False),
Column("fx_rate_used", Numeric(12, 6)),
Column("ticker", Text),
Column("description", Text),
Column("flow_at", DateTime(timezone=True), nullable=False),
Column("raw", JSONB, nullable=False),
Column("broker", Text, nullable=False, server_default=text("'FUTU'")),
Column("account_env", Text, nullable=False),
Column("broker_account", Text, nullable=False),
CheckConstraint("broker = 'FUTU'", name="ck_futu_cash_flow_broker"),
CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')",
name="ck_futu_cash_flow_env"),
CheckConstraint(
"flow_type IN ('deposit','withdraw','dividend','interest','fee','other')",
name="ck_futu_cash_flow_type"),
UniqueConstraint("broker", "account_env", "broker_account", "futu_flow_id",
name="uq_futu_cash_flow_scope_flow"),
Index("ix_futu_cash_flow_scope_at", "broker", "account_env",
"broker_account", "flow_at"),
schema="xenon",
)

corporate_actions = Table(
"corporate_actions", xenon_metadata,
Column("id", BigInteger, primary_key=True),
Column("ticker", Text, nullable=False),
Column("action_type", Text, nullable=False),
Column("effective_date", Date, nullable=False),
Column("ratio_numerator", Numeric(8, 4)),
Column("ratio_denominator", Numeric(8, 4)),
Column("new_ticker", Text),
Column("source", Text, nullable=False, server_default=text("'FUTU_GET_REHAB'")),
Column("notes", JSONB),
UniqueConstraint("ticker", "action_type", "effective_date",
name="uq_corp_actions"),
Index("ix_corp_actions_ticker_date", "ticker", "effective_date"),
schema="xenon",
)

futu_reconciliation_log = Table(
"futu_reconciliation_log", xenon_metadata,
Column("id", BigInteger, primary_key=True),
Column("kind", Text, nullable=False),
Column("recon_date", Date, nullable=False),
Column("reported_nav", Numeric(14, 2)),
Column("reconstructed_nav", Numeric(14, 2)),
Column("delta", Numeric(14, 2)),
Column("threshold", Numeric(14, 2)),
Column("exceeded", Boolean),
Column("notes", JSONB),
Column("computed_at", DateTime(timezone=True), nullable=False,
server_default=text("now()")),
Column("broker", Text, nullable=False, server_default=text("'FUTU'")),
Column("account_env", Text, nullable=False),
Column("broker_account", Text, nullable=False),
CheckConstraint("kind IN ('daily','anomaly')", name="ck_futu_recon_kind"),
schema="xenon",
)

fx_daily = Table(
"fx_daily", xenon_metadata,
Column("currency", Text, primary_key=True),
Column("date", Date, primary_key=True),
Column("currency_per_usd", Numeric(12, 6), nullable=False),
Column("source", Text, nullable=False, server_default=text("'FRED'")),
schema="xenon",
)

````

- [ ] **Step 5: Write the Alembic migration**

Create `src/xenon/db/migrations/versions/2026_06_01_0001_futu_history_tables.py`. Use `uv run alembic current` first to find the previous revision id, then plug it into `down_revision`.

```bash
uv run alembic current
````

```python
"""futu history tables

Revision ID: 2026_06_01_0001
Revises: <PUT_PREVIOUS_REVISION_ID_HERE_FROM_alembic_current>
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "2026_06_01_0001"
down_revision = "<paste from alembic current>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "futu_trades",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("futu_deal_id", sa.Text, nullable=False),
        sa.Column("futu_order_id", sa.Text, nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("futu_code", sa.Text, nullable=False),
        sa.Column("market", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("fees", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(12, 2)),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("broker", sa.Text, nullable=False,
                  server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_trades_broker"),
        sa.CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')",
                           name="ck_futu_trades_env"),
        sa.CheckConstraint("market = 'US'", name="ck_futu_trades_market_us"),
        sa.UniqueConstraint("broker", "account_env", "broker_account", "futu_deal_id",
                            name="uq_futu_trades_scope_deal"),
        schema="xenon",
    )
    op.create_index("ix_futu_trades_scope_filled", "futu_trades",
                    ["broker", "account_env", "broker_account", "filled_at"],
                    schema="xenon")
    op.create_index("ix_futu_trades_ticker_filled", "futu_trades",
                    ["ticker", "filled_at"], schema="xenon")

    op.create_table(
        "futu_cash_flow",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("futu_flow_id", sa.Text, nullable=False),
        sa.Column("flow_type", sa.Text, nullable=False),
        sa.Column("futu_type_raw", sa.Text, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2)),  # NULLABLE
        sa.Column("amount_original", sa.Numeric(12, 2), nullable=False),
        sa.Column("original_currency", sa.Text, nullable=False),
        sa.Column("fx_rate_used", sa.Numeric(12, 6)),
        sa.Column("ticker", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("flow_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("broker", sa.Text, nullable=False,
                  server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_cash_flow_broker"),
        sa.CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')",
                           name="ck_futu_cash_flow_env"),
        sa.CheckConstraint(
            "flow_type IN ('deposit','withdraw','dividend','interest','fee','other')",
            name="ck_futu_cash_flow_type"),
        sa.UniqueConstraint("broker", "account_env", "broker_account", "futu_flow_id",
                            name="uq_futu_cash_flow_scope_flow"),
        schema="xenon",
    )
    op.create_index("ix_futu_cash_flow_scope_at", "futu_cash_flow",
                    ["broker", "account_env", "broker_account", "flow_at"],
                    schema="xenon")

    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("ratio_numerator", sa.Numeric(8, 4)),
        sa.Column("ratio_denominator", sa.Numeric(8, 4)),
        sa.Column("new_ticker", sa.Text),
        sa.Column("source", sa.Text, nullable=False,
                  server_default=sa.text("'FUTU_GET_REHAB'")),
        sa.Column("notes", JSONB),
        sa.UniqueConstraint("ticker", "action_type", "effective_date",
                            name="uq_corp_actions"),
        schema="xenon",
    )
    op.create_index("ix_corp_actions_ticker_date", "corporate_actions",
                    ["ticker", "effective_date"], schema="xenon")

    op.create_table(
        "futu_reconciliation_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("recon_date", sa.Date, nullable=False),
        sa.Column("reported_nav", sa.Numeric(14, 2)),
        sa.Column("reconstructed_nav", sa.Numeric(14, 2)),
        sa.Column("delta", sa.Numeric(14, 2)),
        sa.Column("threshold", sa.Numeric(14, 2)),
        sa.Column("exceeded", sa.Boolean),
        sa.Column("notes", JSONB),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("broker", sa.Text, nullable=False,
                  server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("kind IN ('daily','anomaly')", name="ck_futu_recon_kind"),
        schema="xenon",
    )
    op.create_index(
        "uq_futu_recon_daily",
        "futu_reconciliation_log",
        ["broker", "account_env", "broker_account", "recon_date"],
        unique=True,
        postgresql_where=sa.text("kind = 'daily'"),
        schema="xenon",
    )
    op.create_index("ix_futu_recon_kind_date", "futu_reconciliation_log",
                    ["kind", "recon_date"], schema="xenon")

    op.create_table(
        "fx_daily",
        sa.Column("currency", sa.Text, primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("currency_per_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("source", sa.Text, nullable=False,
                  server_default=sa.text("'FRED'")),
        schema="xenon",
    )

    op.add_column(
        "nav_history",
        sa.Column("source", sa.Text, nullable=False,
                  server_default=sa.text("'reported'")),
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_history_source",
        "nav_history",
        "source IN ('reported','reconstructed')",
        schema="xenon",
    )

    # all_fills view — autogenerate doesn't handle views.
    op.execute("""
        CREATE OR REPLACE VIEW xenon.all_fills AS
        SELECT 'IB'::text AS broker,
               f.account_env, f.broker_account,
               f.ticker, f.side AS action, f.qty AS quantity,
               f.price, f.commission AS fees, f.filled_at,
               NULL::numeric AS realized_pnl, NULL::jsonb AS raw
        FROM xenon.order_fills f WHERE f.broker = 'IB'
        UNION ALL
        SELECT 'FUTU'::text AS broker,
               t.account_env, t.broker_account,
               t.ticker, t.action, t.quantity,
               t.price, t.fees, t.filled_at,
               t.realized_pnl, t.raw
        FROM xenon.futu_trades t;
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS xenon.all_fills")
    op.drop_constraint("ck_nav_history_source", "nav_history",
                       type_="check", schema="xenon")
    op.drop_column("nav_history", "source", schema="xenon")
    op.drop_table("fx_daily", schema="xenon")
    op.drop_index("ix_futu_recon_kind_date",
                  table_name="futu_reconciliation_log", schema="xenon")
    op.drop_index("uq_futu_recon_daily",
                  table_name="futu_reconciliation_log", schema="xenon")
    op.drop_table("futu_reconciliation_log", schema="xenon")
    op.drop_index("ix_corp_actions_ticker_date",
                  table_name="corporate_actions", schema="xenon")
    op.drop_table("corporate_actions", schema="xenon")
    op.drop_index("ix_futu_cash_flow_scope_at",
                  table_name="futu_cash_flow", schema="xenon")
    op.drop_table("futu_cash_flow", schema="xenon")
    op.drop_index("ix_futu_trades_ticker_filled",
                  table_name="futu_trades", schema="xenon")
    op.drop_index("ix_futu_trades_scope_filled",
                  table_name="futu_trades", schema="xenon")
    op.drop_table("futu_trades", schema="xenon")
```

- [ ] **Step 6: Apply migration on the test DB and run smoke test**

```bash
DATABASE_URL="$DATABASE_URL_TEST" uv run alembic upgrade head
uv run pytest scripts/tests/test_futu_migration.py -xvs
```

Expected: 7 tests pass.

- [ ] **Step 6b: Extend the autouse truncate list in `scripts/tests/conftest.py`**

`scripts/tests/conftest.py:59` defines `_truncate_postgres_tables()` invoked by
the autouse `_postgres_orders_test_db` fixture before/after every test. The
hard-coded `_TABLES` tuple must include the new Futu tables or (a) leaked test
data contaminates other tests and (b) the truncate raises (missing table)
which pins `_PG_REACHABLE_CACHE = False` and silently skips PG for the rest
of the suite.

Insert these lines just before the closing `):` of the `_TABLES` tuple:

```python
                    "xenon.futu_trades",
                    "xenon.futu_cash_flow",
                    "xenon.corporate_actions",
                    "xenon.futu_reconciliation_log",
                    "xenon.fx_daily",
```

CI prerequisite: the test workflow must run `uv run alembic upgrade head`
against `core_test` BEFORE `uv run pytest`. Verify in `.github/workflows/ci.yml`.

- [ ] **Step 7: Verify downgrade is clean, then re-upgrade**

```bash
DATABASE_URL="$DATABASE_URL_TEST" uv run alembic downgrade -1
DATABASE_URL="$DATABASE_URL_TEST" uv run alembic upgrade head
uv run pytest scripts/tests/test_futu_migration.py -xvs
```

Expected: downgrade and re-upgrade both succeed; tests still pass.

- [ ] **Step 8: Stage and stop (per CLAUDE.md commit policy)**

```bash
git add scripts/tests/test_futu_migration.py \
        scripts/tests/conftest.py \
        src/xenon/db/migrations/versions/2026_06_01_0001_futu_history_tables.py \
        src/xenon/db/schema.py \
        src/xenon/api/services/advisory_lock.py
git status
```

**Do not commit.** Stop here and surface the staged diff to the user; let them run the commit when ready.

---

### Task 2: NYSE trading-calendar helper

**Files:**

- Create: `src/xenon/utils/market_calendar.py`
- Test: `scripts/tests/test_market_calendar.py`
- Modify: `pyproject.toml` (add `pandas_market_calendars` dependency)

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_market_calendar.py
from datetime import date
from xenon.utils.market_calendar import (
    is_trading_day, previous_trading_day, trading_days_in_range,
)


def test_is_trading_day_weekday():
    assert is_trading_day(date(2024, 5, 1)) is True   # Wed


def test_is_trading_day_weekend():
    assert is_trading_day(date(2024, 5, 4)) is False  # Sat


def test_is_trading_day_us_holiday():
    assert is_trading_day(date(2024, 7, 4)) is False  # Independence Day


def test_previous_trading_day_skips_weekend():
    # Monday 2024-05-06 → previous trading day is Friday 2024-05-03
    assert previous_trading_day(date(2024, 5, 6)) == date(2024, 5, 3)


def test_previous_trading_day_skips_holiday():
    # Day after July 4 (a Thursday holiday) → Wed July 3.
    assert previous_trading_day(date(2024, 7, 5)) == date(2024, 7, 3)


def test_trading_days_in_range_inclusive():
    days = trading_days_in_range(date(2024, 7, 1), date(2024, 7, 5))
    # July 1 Mon, July 2 Tue, July 3 Wed, July 5 Fri  (July 4 is the holiday)
    assert days == [date(2024, 7, 1), date(2024, 7, 2),
                    date(2024, 7, 3), date(2024, 7, 5)]
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_market_calendar.py -xvs
```

Expected: `ModuleNotFoundError: No module named 'xenon.utils.market_calendar'`.

- [ ] **Step 3: Add the dependency**

```bash
uv add pandas_market_calendars
```

- [ ] **Step 4: Implement the helper**

```python
# src/xenon/utils/market_calendar.py
"""Thin NYSE trading-calendar wrapper.

Cached at module level — pandas_market_calendars init costs ~50ms per call,
and the calendar is process-immutable.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import List

import pandas_market_calendars as mcal


@lru_cache(maxsize=1)
def _nyse():
    return mcal.get_calendar("NYSE")


@lru_cache(maxsize=4096)
def is_trading_day(d: date) -> bool:
    sched = _nyse().schedule(start_date=d.isoformat(), end_date=d.isoformat())
    return not sched.empty


def previous_trading_day(d: date) -> date:
    cursor = d - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def trading_days_in_range(start: date, end: date) -> List[date]:
    """Inclusive on both ends. Returns dates in ascending order."""
    sched = _nyse().schedule(start_date=start.isoformat(),
                             end_date=end.isoformat())
    return [ts.date() for ts in sched.index]
```

- [ ] **Step 5: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_market_calendar.py -xvs
```

Expected: 6 pass.

- [ ] **Step 6: Stage**

```bash
git add src/xenon/utils/market_calendar.py \
        scripts/tests/test_market_calendar.py \
        pyproject.toml uv.lock
git status
```

Stop. Let the user commit.

---

### Task 3: FRED FX backfill module + `xenon.fx_daily` writer

**Files:**

- Create: `src/xenon/utils/futu_fx.py`
- Test: `scripts/tests/test_fx_daily_direction.py`
- Test: `scripts/tests/test_fx_backfill_fred_failure.py`

- [ ] **Step 1: Write the direction test**

```python
# scripts/tests/test_fx_daily_direction.py
"""Pins the FX conversion convention: divide original by currency_per_usd."""
from datetime import date
from decimal import Decimal

import pytest

from xenon.utils.futu_fx import convert_to_usd, FxRateMissing


def test_hkd_780_at_7_8_yields_100_usd(seed_fx):
    seed_fx("HKD", date(2024, 5, 1), Decimal("7.8"))
    usd = convert_to_usd(Decimal("780"), "HKD", date(2024, 5, 1))
    assert usd == Decimal("100.00")


def test_cny_700_at_7_yields_100_usd(seed_fx):
    seed_fx("CNY", date(2024, 5, 1), Decimal("7.0"))
    usd = convert_to_usd(Decimal("700"), "CNY", date(2024, 5, 1))
    assert usd == Decimal("100.00")


def test_usd_passthrough(seed_fx):
    # USD → USD never touches fx_daily.
    usd = convert_to_usd(Decimal("123.45"), "USD", date(2024, 5, 1))
    assert usd == Decimal("123.45")


def test_missing_rate_raises_fx_rate_missing():
    with pytest.raises(FxRateMissing):
        convert_to_usd(Decimal("100"), "HKD", date(1999, 1, 1))
```

Add `scripts/tests/conftest.py` fixture (append if file exists, create otherwise):

```python
# scripts/tests/conftest.py — APPEND, do not overwrite an existing conftest
import os
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def seed_fx():
    """Insert an fx_daily row; auto-cleans after test."""
    url = os.environ["DATABASE_URL_TEST"].replace("+asyncpg", "")
    engine = create_engine(url, future=True)
    inserted: list[tuple[str, date]] = []

    def _seed(currency: str, d: date, rate: Decimal) -> None:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO xenon.fx_daily (currency, date, currency_per_usd) "
                "VALUES (:c, :d, :r) "
                "ON CONFLICT (currency, date) DO UPDATE SET "
                "currency_per_usd = EXCLUDED.currency_per_usd"
            ), {"c": currency, "d": d, "r": rate})
        inserted.append((currency, d))

    yield _seed

    with engine.begin() as conn:
        for c, d in inserted:
            conn.execute(text("DELETE FROM xenon.fx_daily "
                              "WHERE currency=:c AND date=:d"),
                         {"c": c, "d": d})
```

- [ ] **Step 2: Write the backfill-failure test**

```python
# scripts/tests/test_fx_backfill_fred_failure.py
"""FRED-unreachable backfill must abort loudly, not write NULL amounts."""
from datetime import date
from unittest.mock import patch, MagicMock

import httpx
import pytest

from xenon.utils.futu_fx import backfill_fx_series, FxBackfillFailed


def test_backfill_aborts_on_503_after_retries():
    response_503 = httpx.Response(503, request=httpx.Request("GET", "x"))

    with patch("xenon.utils.futu_fx._http_get") as mock_get:
        mock_get.return_value = response_503
        with pytest.raises(FxBackfillFailed) as exc:
            backfill_fx_series("HKD", date(2024, 5, 1), date(2024, 5, 3))

    assert exc.value.reason_code == "fx_backfill_failed"
    # Retries exactly 3× per spec
    assert mock_get.call_count == 3


def test_backfill_writes_rows_on_success():
    csv_body = (
        "observation_date,DEXHKUS\n"
        "2024-05-01,7.8234\n"
        "2024-05-02,7.8245\n"
        "2024-05-03,7.8201\n"
    )
    fake = httpx.Response(200, content=csv_body.encode(),
                          request=httpx.Request("GET", "x"))
    with patch("xenon.utils.futu_fx._http_get", return_value=fake):
        rows_written = backfill_fx_series("HKD",
                                          date(2024, 5, 1),
                                          date(2024, 5, 3))
    assert rows_written == 3
```

- [ ] **Step 3: Run both tests to confirm failure**

```bash
uv run pytest scripts/tests/test_fx_daily_direction.py \
              scripts/tests/test_fx_backfill_fred_failure.py -xvs
```

Expected: `ModuleNotFoundError: No module named 'xenon.utils.futu_fx'`.

- [ ] **Step 4: Implement futu_fx.py**

```python
# src/xenon/utils/futu_fx.py
"""FRED FX backfill + cached USD conversion against xenon.fx_daily.

Direction convention: currency_per_usd stores the foreign-currency-per-USD
rate (FRED's native form). amount_usd = amount_original / currency_per_usd.
"""
from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

import httpx
from sqlalchemy import create_engine, text

FRED_SERIES = {"HKD": "DEXHKUS", "CNY": "DEXCHUS"}
_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_RETRIES = 3
_BACKOFF_S = (1.0, 2.0, 4.0)


class FxRateMissing(Exception):
    """No fx_daily row for (currency, date)."""


@dataclass
class FxBackfillFailed(Exception):
    reason_code: str = "fx_backfill_failed"

    def __str__(self) -> str:  # pragma: no cover
        return self.reason_code


def get_sync_engine():
    url = (os.environ.get("DATABASE_URL_TEST")
           or os.environ["DATABASE_URL"]).replace("+asyncpg", "")
    return create_engine(url, future=True)


def _http_get(url: str, params: dict) -> httpx.Response:
    """Indirection seam so tests can patch the HTTP boundary."""
    with httpx.Client(timeout=30.0) as client:
        return client.get(url, params=params)


def _fetch_fred_csv(series_id: str, start: date, end: date) -> str:
    params = {"id": series_id,
              "cosd": start.isoformat(),
              "coed": end.isoformat()}
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = _http_get(_FRED_URL, params)
            if resp.status_code == 200:
                return resp.text
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp)
        except httpx.HTTPError as e:
            last_exc = e
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF_S[attempt])
    raise FxBackfillFailed()


def _parse_fred_csv(body: str, series_id: str) -> Iterable[tuple[date, Decimal]]:
    reader = csv.DictReader(io.StringIO(body))
    for row in reader:
        rate_str = row.get(series_id, "").strip()
        if rate_str in {"", ".", "ND"}:
            continue  # FRED uses "." for no-observation days
        d = date.fromisoformat(row["observation_date"])
        yield d, Decimal(rate_str)


def backfill_fx_series(currency: str, start: date, end: date) -> int:
    """Pull series from FRED, upsert into xenon.fx_daily. Returns row count."""
    series_id = FRED_SERIES[currency]
    body = _fetch_fred_csv(series_id, start, end)
    rows = list(_parse_fred_csv(body, series_id))
    eng = get_sync_engine()
    with eng.begin() as conn:
        for d, rate in rows:
            conn.execute(text(
                "INSERT INTO xenon.fx_daily (currency, date, currency_per_usd, source) "
                "VALUES (:c, :d, :r, 'FRED') "
                "ON CONFLICT (currency, date) DO UPDATE SET "
                "currency_per_usd = EXCLUDED.currency_per_usd"
            ), {"c": currency, "d": d, "r": rate})
    return len(rows)


def convert_to_usd(amount: Decimal, currency: str, d: date) -> Decimal:
    """Divide original by currency_per_usd. Raises FxRateMissing if absent."""
    if currency == "USD":
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    eng = get_sync_engine()
    with eng.connect() as conn:
        rate = conn.execute(text(
            "SELECT currency_per_usd FROM xenon.fx_daily "
            "WHERE currency=:c AND date=:d"
        ), {"c": currency, "d": d}).scalar_one_or_none()
    if rate is None:
        raise FxRateMissing(f"no fx_daily row for {currency} on {d}")
    return (amount / Decimal(rate)).quantize(Decimal("0.01"),
                                             rounding=ROUND_HALF_UP)
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_fx_daily_direction.py \
              scripts/tests/test_fx_backfill_fred_failure.py -xvs
```

Expected: 6 pass.

- [ ] **Step 6: Stage**

```bash
git add src/xenon/utils/futu_fx.py \
        scripts/tests/test_fx_daily_direction.py \
        scripts/tests/test_fx_backfill_fred_failure.py \
        scripts/tests/conftest.py
git status
```

Stop.

---

### Task 4: Futu API stats observability singleton

**Files:**

- Create: `src/xenon/utils/futu_api_stats.py`
- Test: `scripts/tests/test_futu_api_stats.py`

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_api_stats.py
from xenon.utils.futu_api_stats import FutuApiStats


def test_singleton():
    a = FutuApiStats.instance()
    b = FutuApiStats.instance()
    assert a is b


def test_record_and_snapshot():
    stats = FutuApiStats.instance()
    stats.reset()
    stats.record_call("history_deal_list_query", latency_s=0.42, ok=True)
    stats.record_call("history_deal_list_query", latency_s=0.50, ok=True)
    stats.record_call("history_deal_list_query", latency_s=1.0, ok=False,
                      error_code="rate_limit")
    snap = stats.snapshot()
    fn = snap["history_deal_list_query"]
    assert fn["calls"] == 3
    assert fn["errors"] == 1
    assert abs(fn["mean_latency_s"] - 0.64) < 0.01
    assert fn["error_codes"]["rate_limit"] == 1
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_api_stats.py -xvs
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement (mirror uw_api_stats.py)**

```python
# src/xenon/utils/futu_api_stats.py
"""Thread-safe per-call latency / error counter, mirrors uw_api_stats.py."""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class FutuApiStats:
    _instance: "FutuApiStats | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "latency_sum_s": 0.0,
                     "error_codes": defaultdict(int)})

    @classmethod
    def instance(cls) -> "FutuApiStats":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def record_call(self, fn: str, latency_s: float, ok: bool,
                    error_code: str | None = None) -> None:
        with self._lock:
            row = self._calls[fn]
            row["calls"] += 1
            row["latency_sum_s"] += latency_s
            if not ok:
                row["errors"] += 1
                if error_code:
                    row["error_codes"][error_code] += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for fn, row in self._calls.items():
                calls = row["calls"]
                out[fn] = {
                    "calls": calls,
                    "errors": row["errors"],
                    "mean_latency_s": (row["latency_sum_s"] / calls)
                                      if calls else 0.0,
                    "error_codes": dict(row["error_codes"]),
                }
            return out

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_api_stats.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/utils/futu_api_stats.py \
        scripts/tests/test_futu_api_stats.py
```

Stop.

---

## Milestone 2 — Clients (Tasks 5-6)

### Task 5: `FutuHistoryFetcher` — trade + cash-flow + order paginator

**Files:**

- Create: `src/xenon/clients/futu_history_fetcher.py`
- Test: `scripts/tests/test_futu_history_fetcher.py`
- Test: `scripts/tests/test_futu_dt_parsing.py` (pinning helper TZ behavior)

- [ ] **Step 0: Empirically verify Futu `create_time` time zone + fee column names**

`history_deal_list_query` returns `create_time` as a naive `'YYYY-MM-DD HH:MM:SS'`
string. We need to confirm Futu emits NY-local (the plan's assumption) vs UTC
vs machine-local before locking `_parse_ny_dt`. Same risk for fee column
names — `_sum_fees` enumerates several Futu may return, but Futu releases
have added/renamed fee columns historically.

Run this probe ONCE before implementing the helper. Requires the local Futu
OpenD running on `127.0.0.1:11111`:

```bash
FUTU_ACCOUNT=<your-acc-id> uv run python -c "
from futu import OpenSecTradeContext, TrdMarket, TrdEnv
import os
from datetime import date, timedelta
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                          host='127.0.0.1', port=11111)
start = (date.today() - timedelta(days=7)).isoformat()
end   = date.today().isoformat()
ret, df = ctx.history_deal_list_query(
    start=f'{start} 00:00:00', end=f'{end} 23:59:59',
    acc_id=os.environ['FUTU_ACCOUNT'], trd_env=TrdEnv.REAL)
print('CREATE_TIME SAMPLE:', df[['code','trd_side','create_time','price','qty']].head(10))
print('AVAILABLE COLUMNS:', list(df.columns))
print('FEE COLUMNS:', [c for c in df.columns if 'fee' in c.lower() or 'commission' in c.lower()])
ctx.close()
"
```

Cross-check the printed `create_time` against the wall-clock the Futu app
shows for the same fills. Two outcomes:

1. **Match NY time** → the implementation below is correct; leave
   `_parse_ny_dt` as-is.
2. **They differ by 4-8 hours** → Futu emitted UTC. Rewrite `_parse_ny_dt`:
   ```python
   def _parse_ny_dt(s: str) -> datetime:
       from zoneinfo import ZoneInfo
       naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
       return (naive.replace(tzinfo=timezone.utc)
                    .astimezone(ZoneInfo("America/New_York")))
   ```

For the fee columns: compare the printed `FEE COLUMNS:` line against
`_FEE_COLUMNS` in the helper code below. If any fee-suffix column appears
that's not in `_FEE_COLUMNS`, add it. Conversely, log a warning when a row
contains a fee-suffix column that's NOT in the list.

Either way, pin the chosen behavior in a test:

```python
# scripts/tests/test_futu_dt_parsing.py
from datetime import datetime
from zoneinfo import ZoneInfo
from xenon.clients.futu_history_fetcher import _parse_ny_dt

def test_parse_ny_dt_returns_aware_in_ny():
    dt = _parse_ny_dt("2024-05-01 10:30:00")
    assert dt.tzinfo is not None
    assert dt.tzinfo.utcoffset(dt) == ZoneInfo(
        "America/New_York").utcoffset(dt)
```

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_history_fetcher.py
"""Wraps OpenSecTradeContext.history_deal_list_query / history_order_list_query /
get_acc_cash_flow with paging, US-market filter, rate-limit retry, malformed-row
tolerance."""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_history_fetcher import FutuHistoryFetcher


def _make_deals_page(rows):
    return (0, pd.DataFrame(rows))   # Futu SDK returns (ret_code, dataframe)


def test_fetch_deals_filters_non_us():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page([
        {"deal_id": "1", "order_id": "10", "code": "US.AAPL",
         "trd_side": "BUY", "qty": 100, "price": 150.0,
         "create_time": "2024-05-01 10:00:00", "counter_broker_id": "",
         "currency": "USD"},
        {"deal_id": "2", "order_id": "11", "code": "HK.00700",
         "trd_side": "BUY", "qty": 100, "price": 300.0,
         "create_time": "2024-05-01 10:01:00", "counter_broker_id": "",
         "currency": "HKD"},
    ])
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1),
                               acc_id="acct-1"))
    assert [x.futu_deal_id for x in fills] == ["1"]
    assert fills[0].market == "US"
    assert fills[0].ticker == "AAPL"


def test_fetch_deals_paginates_across_90_day_chunks():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page([])
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    list(f.fetch_deals(date(2024, 1, 1), date(2024, 12, 31), acc_id="acct-1"))
    # 365 / 90 → 5 chunks
    assert sdk.history_deal_list_query.call_count == 5


def test_fetch_deals_retries_then_raises_on_rate_limit():
    sdk = MagicMock()
    sdk.history_deal_list_query.side_effect = [
        (-1, "API rate limit exceeded"),
        (-1, "API rate limit exceeded"),
        (-1, "API rate limit exceeded"),
    ]
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL",
                           retry_sleep_s=0.0, max_retries=3)
    with pytest.raises(RuntimeError, match="rate limit"):
        list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1),
                           acc_id="acct-1"))


def test_fetch_deals_skips_malformed_rows():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page([
        {"deal_id": "1", "order_id": "10", "code": "US.AAPL",
         "trd_side": "BUY", "qty": 100, "price": 150.0,
         "create_time": "2024-05-01 10:00:00", "currency": "USD"},
        {"deal_id": None, "code": "US.MSFT"},
    ])
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1),
                               acc_id="acct-1"))
    assert len(fills) == 1


def test_fetch_cash_flow_returns_typed_rows():
    sdk = MagicMock()
    sdk.get_acc_cash_flow.return_value = (0, pd.DataFrame([
        {"flow_id": "f1", "cashflow_type": "FUND_IN", "amount": 1000.0,
         "currency": "USD", "remark": "wire", "cashflow_remark": "",
         "create_time": "2024-05-01 09:00:00"},
    ]))
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    flows = list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 1),
                                   acc_id="acct-1"))
    assert flows[0].futu_flow_id == "f1"
    assert flows[0].futu_type_raw == "FUND_IN"
    assert float(flows[0].amount_original) == 1000.0
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_history_fetcher.py -xvs
```

Expected: `ModuleNotFoundError: No module named 'xenon.clients.futu_history_fetcher'`.

- [ ] **Step 3: Implement the fetcher**

```python
# src/xenon/clients/futu_history_fetcher.py
"""Read-only history fetcher for Futu OpenSecTradeContext.

Wraps history_deal_list_query / history_order_list_query / get_acc_cash_flow with
90-day paging, US-market filter, rate-limit retry, malformed-row tolerance.
Records per-call stats via FutuApiStats.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator

from xenon.utils.futu_api_stats import FutuApiStats

log = logging.getLogger(__name__)
_PAGE_DAYS = 90
_RATE_LIMIT_KEYWORDS = ("rate limit", "frequency control", "too frequent")

# Sum ALL fee components rather than picking one; Futu may return commission +
# platform fee + clearing fee on the same row. Update this list if Step 0
# empirical probe reveals a Futu SDK field name not yet covered.
_FEE_COLUMNS = ("counter_broker_commission", "platform_fee",
                "clearing_fee", "commission", "fee")


def _sum_fees(row: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for col in _FEE_COLUMNS:
        v = row.get(col)
        if v in (None, ""):
            continue
        try:
            total += Decimal(str(v))
        except Exception:
            continue
    return total


@dataclass
class FutuFill:
    futu_deal_id: str
    futu_order_id: str
    ticker: str
    futu_code: str
    market: str
    action: str
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime
    raw: dict[str, Any]


@dataclass
class FutuCashFlowRow:
    futu_flow_id: str
    futu_type_raw: str
    amount_original: Decimal
    original_currency: str
    description: str | None
    ticker: str | None
    flow_at: datetime
    raw: dict[str, Any]


def _normalize_code(code: str) -> tuple[str, str]:
    if not code or "." not in code:
        return "", code
    market, sym = code.split(".", 1)
    return market.upper(), sym.upper()


def _parse_ny_dt(s: str) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("America/New_York"))


def _is_rate_limit_error(message: str) -> bool:
    msg = (message or "").lower()
    return any(k in msg for k in _RATE_LIMIT_KEYWORDS)


class FutuHistoryFetcher:
    def __init__(self, trade_ctx, trd_env: str = "REAL",
                 max_retries: int = 3, retry_sleep_s: float = 2.0) -> None:
        self._ctx = trade_ctx
        self._trd_env = trd_env
        self._max_retries = max_retries
        self._retry_sleep_s = retry_sleep_s
        self._stats = FutuApiStats.instance()

    def _chunks(self, start: date, end: date) -> Iterator[tuple[date, date]]:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=_PAGE_DAYS - 1), end)
            yield cursor, chunk_end
            cursor = chunk_end + timedelta(days=1)

    def _call_with_retry(self, fn_name: str, fn, *args, **kwargs):
        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            ret = fn(*args, **kwargs)
            latency = time.monotonic() - t0
            if isinstance(ret, tuple) and len(ret) == 2:
                code, payload = ret
            else:
                code, payload = -1, str(ret)
            if code == 0:
                self._stats.record_call(fn_name, latency, ok=True)
                return payload
            if _is_rate_limit_error(str(payload)):
                self._stats.record_call(fn_name, latency, ok=False,
                                        error_code="rate_limit")
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_sleep_s * (2 ** attempt))
                    continue
                raise RuntimeError(f"{fn_name}: rate limit after retries")
            self._stats.record_call(fn_name, latency, ok=False,
                                    error_code=f"code_{code}")
            raise RuntimeError(f"{fn_name} failed: code={code} payload={payload!r}")
        raise RuntimeError(f"{fn_name}: max retries exhausted")

    def fetch_deals(self, start: date, end: date, acc_id: str) -> Iterator[FutuFill]:
        for s, e in self._chunks(start, end):
            df = self._call_with_retry(
                "history_deal_list_query",
                self._ctx.history_deal_list_query,
                start=s.strftime("%Y-%m-%d 00:00:00"),
                end=e.strftime("%Y-%m-%d 23:59:59"),
                acc_id=acc_id, trd_env=self._trd_env,
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict(orient="records"):
                fill = self._row_to_fill(row)
                if fill is None or fill.market != "US":
                    continue
                yield fill

    def _row_to_fill(self, row: dict[str, Any]) -> FutuFill | None:
        deal_id = row.get("deal_id")
        if not deal_id:
            log.warning("dropping malformed Futu deal row: %s", row)
            return None
        market, ticker = _normalize_code(row.get("code", ""))
        try:
            return FutuFill(
                futu_deal_id=str(deal_id),
                futu_order_id=str(row.get("order_id", "")),
                ticker=ticker, futu_code=row.get("code", ""), market=market,
                action=str(row.get("trd_side", "")).upper(),
                quantity=int(row.get("qty", 0)),
                price=Decimal(str(row.get("price", "0"))),
                fees=_sum_fees(row),
                filled_at=_parse_ny_dt(str(row.get("create_time"))),
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu deal row: %s", row)
            return None

    def fetch_cash_flow(self, start: date, end: date,
                        acc_id: str) -> Iterator[FutuCashFlowRow]:
        for s, e in self._chunks(start, end):
            df = self._call_with_retry(
                "get_acc_cash_flow", self._ctx.get_acc_cash_flow,
                clearing_date_start=s.strftime("%Y-%m-%d"),
                clearing_date_end=e.strftime("%Y-%m-%d"),
                acc_id=acc_id, trd_env=self._trd_env,
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict(orient="records"):
                cf = self._row_to_cash_flow(row)
                if cf is not None:
                    yield cf

    def _row_to_cash_flow(self, row: dict[str, Any]) -> FutuCashFlowRow | None:
        fid = row.get("flow_id") or row.get("cashflow_id")
        if not fid:
            log.warning("dropping malformed Futu cash flow row: %s", row)
            return None
        try:
            return FutuCashFlowRow(
                futu_flow_id=str(fid),
                futu_type_raw=str(row.get("cashflow_type", row.get("type", ""))),
                amount_original=Decimal(str(row.get("amount", "0"))),
                original_currency=str(row.get("currency", "USD")).upper(),
                description=row.get("remark") or row.get("cashflow_remark"),
                ticker=row.get("stock_code") or None,
                flow_at=_parse_ny_dt(str(row.get("create_time",
                                                 row.get("cashflow_time")))),
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu cash flow row: %s", row)
            return None
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_history_fetcher.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/clients/futu_history_fetcher.py \
        scripts/tests/test_futu_history_fetcher.py
```

Stop.

---

### Task 6: `FutuQuoteClient` — `get_rehab` for corporate actions

**Files:**

- Create: `src/xenon/clients/futu_quote_client.py`
- Test: `scripts/tests/test_futu_corporate_actions_get_rehab.py`

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_corporate_actions_get_rehab.py
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_quote_client import FutuQuoteClient


def test_fetch_splits_parses_ratio():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame([
        {"ex_div_date": "2024-06-10",
         "split_ratio": 10.0,
         "join_ratio": 1.0,
         "company_act_flag": 0x04},  # 0x04 = split per Futu docs
    ]))
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    actions = list(c.fetch_corporate_actions("US.NVDA"))
    assert len(actions) == 1
    a = actions[0]
    assert a.ticker == "NVDA"
    assert a.action_type == "split"
    assert a.effective_date == date(2024, 6, 10)
    assert float(a.ratio_numerator) == 10.0
    assert float(a.ratio_denominator) == 1.0


def test_fetch_returns_empty_on_no_rows():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    assert list(c.fetch_corporate_actions("US.AAPL")) == []


def test_fetch_raises_on_sdk_error():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (-1, "API error")
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    with pytest.raises(RuntimeError):
        list(c.fetch_corporate_actions("US.AAPL"))
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_corporate_actions_get_rehab.py -xvs
```

- [ ] **Step 3: Implement**

```python
# src/xenon/clients/futu_quote_client.py
"""Read-only OpenQuoteContext wrapper for get_rehab corporate-action lookups.

Only get_rehab is used. Caller owns OpenQuoteContext lifecycle.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterator

from xenon.utils.futu_api_stats import FutuApiStats

log = logging.getLogger(__name__)
_SPLIT_FLAG = 0x04
_REVERSE_SPLIT_FLAG = 0x08


@dataclass
class CorporateAction:
    ticker: str
    action_type: str
    effective_date: date
    ratio_numerator: Decimal
    ratio_denominator: Decimal
    new_ticker: str | None
    raw: dict[str, Any]


def _normalize(code: str) -> str:
    return code.split(".", 1)[-1].upper() if code else code


class FutuQuoteClient:
    def __init__(self, quote_ctx) -> None:
        self._ctx = quote_ctx
        self._stats = FutuApiStats.instance()

    def fetch_corporate_actions(self, code: str) -> Iterator[CorporateAction]:
        t0 = time.monotonic()
        ret = self._ctx.get_rehab(code)
        latency = time.monotonic() - t0
        if not isinstance(ret, tuple) or ret[0] != 0:
            self._stats.record_call("get_rehab", latency, ok=False,
                                    error_code=str(ret[0] if isinstance(ret, tuple) else "?"))
            raise RuntimeError(f"get_rehab({code}) failed: {ret!r}")
        self._stats.record_call("get_rehab", latency, ok=True)
        df = ret[1]
        if df is None or df.empty:
            return
        ticker = _normalize(code)
        for row in df.to_dict(orient="records"):
            flag = int(row.get("company_act_flag", 0) or 0)
            split = Decimal(str(row.get("split_ratio", "1") or "1"))
            join = Decimal(str(row.get("join_ratio", "1") or "1"))
            if flag & _SPLIT_FLAG:
                yield CorporateAction(
                    ticker=ticker, action_type="split",
                    effective_date=date.fromisoformat(str(row["ex_div_date"])),
                    ratio_numerator=split, ratio_denominator=join,
                    new_ticker=None, raw=row,
                )
            elif flag & _REVERSE_SPLIT_FLAG:
                yield CorporateAction(
                    ticker=ticker, action_type="reverse_split",
                    effective_date=date.fromisoformat(str(row["ex_div_date"])),
                    ratio_numerator=split, ratio_denominator=join,
                    new_ticker=None, raw=row,
                )
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_corporate_actions_get_rehab.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/clients/futu_quote_client.py \
        scripts/tests/test_futu_corporate_actions_get_rehab.py
```

Stop.

---

## Milestone 3 — FIFO matcher (Tasks 7-8)

### Task 7: FIFO matcher core (fills only, no corp actions)

**Files:**

- Create: `src/xenon/execution/futu_fifo_matcher.py`
- Test: `scripts/tests/test_futu_fifo_matcher.py`
- Test: `scripts/tests/test_futu_fifo_stable_order.py`

- [ ] **Step 1: Write the matcher tests**

```python
# scripts/tests/test_futu_fifo_matcher.py
"""Core FIFO: opening fills, full close, partial close, over-close warning,
commission-net accuracy. Corp actions covered in Task 8."""
from datetime import datetime, timezone
from decimal import Decimal
from xenon.execution.futu_fifo_matcher import match_fills, FillInput


def _f(deal_id, action, qty, price, fees="0", t="2024-05-01T10:00:00+00:00"):
    return FillInput(
        futu_deal_id=deal_id, action=action, quantity=qty,
        price=Decimal(str(price)), fees=Decimal(str(fees)),
        filled_at=datetime.fromisoformat(t),
    )


def test_opening_fill_realized_pnl_is_none():
    fills = [_f("1", "BUY", 100, 150)]
    out = match_fills(fills, corp_actions=[])
    assert out[0].realized_pnl is None


def test_full_close_one_lot():
    fills = [_f("1", "BUY", 100, 150, fees="1.00"),
             _f("2", "SELL", 100, 160, fees="1.00",
                t="2024-05-02T10:00:00+00:00")]
    out = match_fills(fills, corp_actions=[])
    # gross = (160-150)*100 = 1000; net of $1 sell fee + $1 buy fee = 998
    assert out[1].realized_pnl == Decimal("998.00")


def test_partial_close_across_two_lots():
    fills = [
        _f("1", "BUY", 50, 150, fees="0.50"),
        _f("2", "BUY", 50, 155, fees="0.50",
           t="2024-05-02T10:00:00+00:00"),
        _f("3", "SELL", 75, 160, fees="0.75",
           t="2024-05-03T10:00:00+00:00"),
    ]
    out = match_fills(fills, corp_actions=[])
    # Closes 50@150 fully + 25@155 partially.
    # gross = (160-150)*50 + (160-155)*25 = 500 + 125 = 625
    # buy fee allocation = 0.50 (full lot1) + 0.50*(25/50) (half of lot2) = 0.75
    # net = 625 - 0.75 (sell) - 0.75 (buy) = 623.50
    assert out[2].realized_pnl == Decimal("623.50")


def test_over_close_logs_warning_no_raise(caplog):
    fills = [_f("1", "BUY", 50, 150),
             _f("2", "SELL", 100, 160,
                t="2024-05-02T10:00:00+00:00")]
    with caplog.at_level("WARNING"):
        out = match_fills(fills, corp_actions=[])
    assert any("closed more than opened" in r.message.lower()
               for r in caplog.records)
    # gross = (160-150)*50 = 500, no fees → 500
    assert out[1].realized_pnl == Decimal("500.00")


def test_lot_history_side_channel_populated():
    fills = [_f("1", "BUY", 100, 150),
             _f("2", "SELL", 100, 160,
                t="2024-05-02T10:00:00+00:00")]
    out = match_fills(fills, corp_actions=[], return_lot_history=True)
    assert hasattr(out, "fills") and hasattr(out, "lot_history")
    assert any(ev["kind"] == "open" for ev in out.lot_history)
    assert any(ev["kind"] == "close" for ev in out.lot_history)
```

```python
# scripts/tests/test_futu_fifo_stable_order.py
"""Same-timestamp fills produce deterministic P&L across input orderings."""
import random
from datetime import datetime
from decimal import Decimal
from xenon.execution.futu_fifo_matcher import match_fills, FillInput

T = "2024-05-01T10:00:00+00:00"


def _f(deal_id, action, qty, price):
    return FillInput(futu_deal_id=deal_id, action=action, quantity=qty,
                     price=Decimal(str(price)), fees=Decimal("0"),
                     filled_at=datetime.fromisoformat(T))


def test_deterministic_across_input_orderings():
    base = [_f("a", "BUY", 50, 150),
            _f("b", "BUY", 50, 155),
            _f("c", "SELL", 75, 160)]
    expected = [(f.futu_deal_id, f.realized_pnl)
                for f in match_fills(base, corp_actions=[])]
    rng = random.Random(0)
    for _ in range(20):
        shuffled = base[:]
        rng.shuffle(shuffled)
        got = [(f.futu_deal_id, f.realized_pnl)
               for f in match_fills(shuffled, corp_actions=[])]
        # Re-sort by deal_id for comparison since output order is event-time.
        assert sorted(got) == sorted(expected)
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_fifo_matcher.py \
              scripts/tests/test_futu_fifo_stable_order.py -xvs
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement core matcher (fills only)**

```python
# src/xenon/execution/futu_fifo_matcher.py
"""Deterministic FIFO lot matcher with corp-action awareness.

Inputs:
  fills:       list[FillInput]      — for one (scope, ticker)
  corp_actions: list[CorpActionInput] — same (scope, ticker)

Outputs:
  Either list[FillOutput] (default) or MatchResult(fills, lot_history)
  when return_lot_history=True. lot_history is consumed by the NAV
  backward walk to reconstruct positions after a ticker-reopen.

Stable order: events sorted by (date, kind_priority, secondary_key) where
fills sort BEFORE corp actions on the same date and the secondary key is
the deal_id / corp action id for tie-breaking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, NamedTuple

log = logging.getLogger(__name__)


@dataclass
class FillInput:
    futu_deal_id: str
    action: str                # 'BUY' | 'SELL'
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime


@dataclass
class CorpActionInput:
    id: str
    action_type: str           # 'split' | 'reverse_split'
    effective_date: date
    ratio_numerator: Decimal   # post / pre
    ratio_denominator: Decimal


@dataclass
class FillOutput:
    futu_deal_id: str
    action: str
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime
    realized_pnl: Decimal | None


@dataclass
class _Lot:
    qty: Decimal
    avg_cost: Decimal
    qty_at_creation: Decimal
    fees_amortized: Decimal


class MatchResult(NamedTuple):
    fills: list[FillOutput]
    lot_history: list[dict]


def _event_sort_key(ev):
    # (date, kind_priority, secondary)
    # fills (kind=0) sort BEFORE corp actions (kind=1) on the same date.
    kind, dt, secondary, _ = ev
    if isinstance(dt, datetime):
        key_date = dt.date()
    else:
        key_date = dt
    kind_pri = 0 if kind == "fill" else 1
    return (key_date, kind_pri, dt if isinstance(dt, datetime) else None,
            secondary)


def match_fills(fills: Iterable[FillInput],
                corp_actions: Iterable[CorpActionInput],
                return_lot_history: bool = False):
    events = [("fill", f.filled_at, f.futu_deal_id, f) for f in fills]
    events += [("corp", c.effective_date, c.id, c) for c in corp_actions]
    events.sort(key=_event_sort_key)

    open_lots: list[_Lot] = []
    out: list[FillOutput] = []
    lot_history: list[dict] = []

    for kind, _dt, _sec, payload in events:
        if kind == "corp":
            ca = payload
            ratio = ca.ratio_numerator / ca.ratio_denominator
            for lot in open_lots:
                lot.qty *= ratio
                lot.qty_at_creation *= ratio
                lot.avg_cost /= ratio
            lot_history.append({"kind": "corp", "date": ca.effective_date,
                                "ratio": float(ratio)})
            continue

        fill: FillInput = payload
        if fill.action == "BUY":
            open_lots.append(_Lot(
                qty=Decimal(fill.quantity),
                avg_cost=fill.price,
                qty_at_creation=Decimal(fill.quantity),
                fees_amortized=fill.fees,
            ))
            out.append(FillOutput(
                futu_deal_id=fill.futu_deal_id, action="BUY",
                quantity=fill.quantity, price=fill.price, fees=fill.fees,
                filled_at=fill.filled_at, realized_pnl=None,
            ))
            lot_history.append({"kind": "open", "deal_id": fill.futu_deal_id,
                                "qty": fill.quantity,
                                "avg_cost": float(fill.price),
                                "filled_at": fill.filled_at.isoformat()})
        else:  # SELL
            remaining = Decimal(fill.quantity)
            gross_pnl = Decimal("0")
            matched_buy_fees = Decimal("0")
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                matched_qty = min(remaining, lot.qty)
                gross_pnl += (fill.price - lot.avg_cost) * matched_qty
                matched_buy_fees += (lot.fees_amortized *
                                     (matched_qty / lot.qty_at_creation))
                lot.qty -= matched_qty
                remaining -= matched_qty
                if lot.qty == 0:
                    open_lots.pop(0)
            if remaining > 0:
                log.warning("closed more than opened (deal_id=%s, remaining=%s)",
                            fill.futu_deal_id, remaining)
            realized = (gross_pnl - fill.fees - matched_buy_fees).quantize(
                Decimal("0.01"))
            out.append(FillOutput(
                futu_deal_id=fill.futu_deal_id, action="SELL",
                quantity=fill.quantity, price=fill.price, fees=fill.fees,
                filled_at=fill.filled_at, realized_pnl=realized,
            ))
            lot_history.append({"kind": "close", "deal_id": fill.futu_deal_id,
                                "qty": fill.quantity,
                                "realized_pnl": float(realized),
                                "filled_at": fill.filled_at.isoformat()})

    if return_lot_history:
        return MatchResult(fills=out, lot_history=lot_history)
    return out
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_fifo_matcher.py \
              scripts/tests/test_futu_fifo_stable_order.py -xvs
```

Expected: 6 pass.

- [ ] **Step 5: Stage**

```bash
git add src/xenon/execution/futu_fifo_matcher.py \
        scripts/tests/test_futu_fifo_matcher.py \
        scripts/tests/test_futu_fifo_stable_order.py
```

Stop.

---

### Task 8: FIFO matcher with corporate actions

**Files:**

- Modify: `src/xenon/execution/futu_fifo_matcher.py` (no new code — already accepts corp_actions; just lock down via tests)
- Test: `scripts/tests/test_futu_fifo_corp_action.py`

- [ ] **Step 1: Write the corp-action tests**

```python
# scripts/tests/test_futu_fifo_corp_action.py
"""Splits/reverse splits applied mid-hold preserve total realized P&L."""
from datetime import datetime, date
from decimal import Decimal

from xenon.execution.futu_fifo_matcher import (
    match_fills, FillInput, CorpActionInput,
)


def _f(deal_id, action, qty, price, t):
    return FillInput(futu_deal_id=deal_id, action=action, quantity=qty,
                     price=Decimal(str(price)), fees=Decimal("0"),
                     filled_at=datetime.fromisoformat(t))


def _ca(id_, type_, d, num, den):
    return CorpActionInput(id=id_, action_type=type_,
                           effective_date=date.fromisoformat(d),
                           ratio_numerator=Decimal(str(num)),
                           ratio_denominator=Decimal(str(den)))


def test_ten_for_one_split_preserves_total_realized():
    fills = [_f("1", "BUY", 10, 1000, "2024-05-01T10:00:00+00:00"),
             # After 10:1 split on 6/10, the position is 100 sh @ $100 cost.
             _f("2", "SELL", 100, 110, "2024-06-15T10:00:00+00:00")]
    corp = [_ca("ca1", "split", "2024-06-10", 10, 1)]
    out = match_fills(fills, corp_actions=corp)
    # gross = (110 - 100) * 100 = 1000
    assert out[1].realized_pnl == Decimal("1000.00")


def test_reverse_split_1_for_5():
    fills = [_f("1", "BUY", 500, 2, "2024-05-01T10:00:00+00:00"),
             # After 1:5 reverse split: 100 sh @ $10 cost
             _f("2", "SELL", 100, 12, "2024-06-15T10:00:00+00:00")]
    corp = [_ca("ca1", "reverse_split", "2024-06-10", 1, 5)]
    out = match_fills(fills, corp_actions=corp)
    # gross = (12 - 10) * 100 = 200
    assert out[1].realized_pnl == Decimal("200.00")


def test_split_on_same_day_sorts_after_fill():
    # BUY on 2024-06-10 morning → split same date → SELL on 2024-06-15.
    # Spec: fills sort BEFORE corp actions on same date.
    fills = [_f("1", "BUY", 10, 1000, "2024-06-10T09:30:00+00:00"),
             _f("2", "SELL", 100, 110, "2024-06-15T10:00:00+00:00")]
    corp = [_ca("ca1", "split", "2024-06-10", 10, 1)]
    out = match_fills(fills, corp_actions=corp)
    assert out[1].realized_pnl == Decimal("1000.00")


def test_two_sequential_splits():
    # Initial: 10 @ $100. Split 2:1 on 6/10 → 20 @ $50. Split 5:1 on 7/01
    # → 100 @ $10. SELL 100 @ $12 → P&L = 200.
    fills = [_f("1", "BUY", 10, 100, "2024-05-01T10:00:00+00:00"),
             _f("2", "SELL", 100, 12, "2024-07-15T10:00:00+00:00")]
    corp = [_ca("ca1", "split", "2024-06-10", 2, 1),
            _ca("ca2", "split", "2024-07-01", 5, 1)]
    out = match_fills(fills, corp_actions=corp)
    assert out[1].realized_pnl == Decimal("200.00")
```

- [ ] **Step 2: Run tests to confirm pass (matcher already accepts corp_actions)**

```bash
uv run pytest scripts/tests/test_futu_fifo_corp_action.py -xvs
```

Expected: 4 pass. If they fail, the bug is in the existing matcher — fix at `src/xenon/execution/futu_fifo_matcher.py` until green.

- [ ] **Step 3: Stage**

```bash
git add scripts/tests/test_futu_fifo_corp_action.py
```

Stop.

---

## Milestone 4 — Database query helpers (Task 9)

### Task 9: `futu_history_queries` — typed DB CRUD for the new tables

**Files:**

- Create: `src/xenon/db/queries/futu_history.py`
- Test: `scripts/tests/test_futu_history_queries.py`

This module owns ALL SQL against the new tables — sync orchestration and FastAPI routes import from here, never write SQL inline. Keeps the SQL surface auditable and lets us evolve schema centrally.

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_history_queries.py
"""Lock down the typed DB CRUD shape used by sync + API."""
from datetime import date, datetime, timezone
from decimal import Decimal
from xenon.db.queries.futu_history import (
    insert_fills,
    insert_cash_flows,
    insert_corporate_actions,
    update_realized_pnl,
    fetch_fills,
    fetch_cash_flows,
    cursor_for_fills,
    cursor_for_cash_flows,
    cursor_for_corp_actions,
    write_nav_row,
    fetch_nav_history,
    write_recon_daily,
    write_recon_anomaly,
)
from xenon.execution.account_scope import AccountScope


SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")


def _fill(deal_id, action, qty, price, t):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action=action, quantity=qty,
        price=Decimal(str(price)), fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t), raw={},
    )


def test_insert_fills_idempotent(clean_scope):
    rows = [_fill("d1", "BUY", 100, 150, "2024-05-01T10:00:00+00:00")]
    n1 = insert_fills(SCOPE, rows)
    n2 = insert_fills(SCOPE, rows)
    assert n1 == 1
    assert n2 == 0


def test_cursor_for_fills_returns_max_date(clean_scope):
    insert_fills(SCOPE, [
        _fill("d1", "BUY", 1, 1, "2024-05-01T10:00:00+00:00"),
        _fill("d2", "BUY", 1, 1, "2024-05-02T10:00:00+00:00"),
    ])
    assert cursor_for_fills(SCOPE) == date(2024, 5, 2)


def test_cursor_returns_none_for_empty_scope(clean_scope):
    assert cursor_for_fills(SCOPE) is None
    assert cursor_for_cash_flows(SCOPE) is None
    assert cursor_for_corp_actions(SCOPE) is None


def test_update_realized_pnl(clean_scope):
    insert_fills(SCOPE, [
        _fill("d1", "BUY", 100, 150, "2024-05-01T10:00:00+00:00"),
        _fill("d2", "SELL", 100, 160, "2024-05-02T10:00:00+00:00"),
    ])
    update_realized_pnl(SCOPE, "d2", Decimal("1000.00"))
    fills = list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 3)))
    by_id = {f["futu_deal_id"]: f for f in fills}
    assert by_id["d1"]["realized_pnl"] is None
    assert by_id["d2"]["realized_pnl"] == Decimal("1000.00")


def test_write_nav_row_then_fetch(clean_scope):
    write_nav_row(SCOPE, recon_date=date(2024, 5, 1), nav=Decimal("10000"),
                  source="reconstructed", cash=Decimal("10000"),
                  stock_value=Decimal("0"))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert rows[0]["nav"] == Decimal("10000")
    assert rows[0]["source"] == "reconstructed"


def test_recon_daily_unique_per_date_anomalies_not(clean_scope):
    write_recon_daily(SCOPE, recon_date=date(2024, 5, 1),
                      reported=Decimal("100"), reconstructed=Decimal("100"),
                      threshold=Decimal("25"))
    # Second daily for same date upserts (does NOT raise).
    write_recon_daily(SCOPE, recon_date=date(2024, 5, 1),
                      reported=Decimal("110"), reconstructed=Decimal("100"),
                      threshold=Decimal("25"))
    # But two anomalies on the same date are both stored.
    write_recon_anomaly(SCOPE, recon_date=date(2024, 5, 1),
                        notes={"anomaly_type": "missing_fx", "currency": "HKD"})
    write_recon_anomaly(SCOPE, recon_date=date(2024, 5, 1),
                        notes={"anomaly_type": "over_close", "ticker": "AAPL"})
```

Add two fixtures to `scripts/tests/conftest.py` (append after the existing
`_trading_mode_paper_default` autouse fixture).

(a) `clean_scope` — wipe scoped + non-scoped tables that tests touch. The
autouse `_postgres_orders_test_db` already truncates most tables, but
`corporate_actions` has no scope columns and `fx_daily` is filled by FX
backfill; clear both explicitly so cross-test contamination cannot bleed in
when a test runs without the autouse (e.g., `-p no:cacheprovider`).

```python
@pytest.fixture
def clean_scope():
    """Wipe Futu-history test-scope rows + non-scoped tables touched by tests."""
    url = os.environ["DATABASE_URL_TEST"].replace("+asyncpg", "")
    engine = create_engine(url, future=True)
    where = ("broker='FUTU' AND account_env='paper' "
             "AND broker_account='pytest'")
    scoped = ["futu_trades", "futu_cash_flow",
              "futu_reconciliation_log", "nav_history", "account_snapshots"]

    def _wipe(conn):
        for t in scoped:
            conn.execute(text(f"DELETE FROM xenon.{t} WHERE {where}"))
        # corporate_actions has no scope columns; clear test tickers.
        conn.execute(text(
            "DELETE FROM xenon.corporate_actions "
            "WHERE ticker = ANY(:tickers)"),
            {"tickers": ["AAPL", "NVDA", "MSFT", "00700"]})
        conn.execute(text(
            "DELETE FROM xenon.fx_daily "
            "WHERE date < DATE '2024-06-01'"))

    with engine.begin() as conn:
        _wipe(conn)
    yield
    with engine.begin() as conn:
        _wipe(conn)
```

(b) `futu_pytest_app_scope` — REQUIRED by every route test in Tasks 17-20.
The autouse `_trading_mode_paper_default` fixture pins
`AccountScope("IB", "paper", "DU0000000")` for all `TestClient(app)` calls;
without an override, `/futu/*` routes that depend on `get_account_scope`
read the IB scope and miss every seeded FUTU row.

```python
@pytest.fixture
def futu_pytest_app_scope(monkeypatch):
    """Force xenon.api.guards.get_account_scope to return the FUTU/pytest scope
    used by the futu-history test fixtures. Routes under test depend on
    Depends(get_account_scope); without this override they read the autouse
    IB/DU0000000 scope and miss the seeded FUTU rows."""
    monkeypatch.setenv("XENON_BROKER", "FUTU")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "pytest")
    try:
        import xenon.api.server as server
        server.app.state.broker = "FUTU"
        server.app.state.account = "pytest"
        server.app.state.trading_mode = "paper"
        server.app.state.mode_verified = True
    except Exception:
        pass
    yield
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_history_queries.py -xvs
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement the query module**

```python
# src/xenon/db/queries/futu_history.py
"""Typed CRUD helpers for the Futu-history tables.

Every SQL string against futu_trades / futu_cash_flow / corporate_actions /
futu_reconciliation_log / nav_history (Futu rows) / fx_daily MUST live here.
Callers (sync service + FastAPI routes) import named functions.

All trade-date partitioning uses America/New_York TZ:
    (filled_at AT TIME ZONE 'America/New_York')::date
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope


def _scope_filter() -> str:
    return ("broker = :broker AND account_env = :env "
            "AND broker_account = :acc")


def _scope_params(scope: AccountScope) -> dict:
    return {"broker": scope.broker, "env": scope.account_env,
            "acc": scope.broker_account}


# ---- fills ---------------------------------------------------------------

_INSERT_FILL = text("""
    INSERT INTO xenon.futu_trades (
        futu_deal_id, futu_order_id, ticker, futu_code, market, action,
        quantity, price, fees, filled_at, raw,
        broker, account_env, broker_account)
    VALUES (:futu_deal_id, :futu_order_id, :ticker, :futu_code, :market,
            :action, :quantity, :price, :fees, :filled_at,
            CAST(:raw AS jsonb), :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, futu_deal_id)
    DO NOTHING
""")


def insert_fills(scope: AccountScope, rows: Iterable[dict]) -> int:
    import json
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(_INSERT_FILL, {
                **_scope_params(scope),
                **{k: r[k] for k in (
                    "futu_deal_id", "futu_order_id", "ticker", "futu_code",
                    "market", "action", "quantity", "price", "fees",
                    "filled_at")},
                "raw": json.dumps(r.get("raw", {}), default=str),
            })
            inserted += res.rowcount
    return inserted


_UPDATE_REALIZED = text("""
    UPDATE xenon.futu_trades
       SET realized_pnl = :pnl
     WHERE broker=:broker AND account_env=:env AND broker_account=:acc
       AND futu_deal_id=:deal_id
""")


def update_realized_pnl(scope: AccountScope, deal_id: str, pnl: Decimal | None) -> None:
    with get_sync_engine().begin() as conn:
        conn.execute(_UPDATE_REALIZED, {**_scope_params(scope),
                                        "deal_id": deal_id, "pnl": pnl})


def null_realized_for_ticker(scope: AccountScope, ticker: str) -> int:
    """Set realized_pnl back to NULL for every SELL of this ticker.
    Used by the late-arrival rematch path in sync step 4."""
    with get_sync_engine().begin() as conn:
        res = conn.execute(text(
            f"UPDATE xenon.futu_trades SET realized_pnl = NULL "
            f"WHERE {_scope_filter()} AND ticker=:ticker AND action='SELL'"
        ), {**_scope_params(scope), "ticker": ticker})
        return res.rowcount


_FETCH_FILLS = text(f"""
    SELECT futu_deal_id, futu_order_id, ticker, futu_code, market, action,
           quantity, price, fees, realized_pnl, filled_at, raw
      FROM xenon.futu_trades
     WHERE {_scope_filter()}
       AND (filled_at AT TIME ZONE 'America/New_York')::date
           BETWEEN :start AND :end
     ORDER BY filled_at, futu_deal_id
""")


def fetch_fills(scope: AccountScope, start: date, end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(_FETCH_FILLS, {**_scope_params(scope),
                                               "start": start, "end": end}):
            yield dict(row._mapping)


def cursor_for_fills(scope: AccountScope) -> date | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT MAX((filled_at AT TIME ZONE 'America/New_York')::date) "
            f"FROM xenon.futu_trades WHERE {_scope_filter()}"
        ), _scope_params(scope)).scalar()


def newest_realized_pnl_at(scope: AccountScope, ticker: str) -> datetime | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT MAX(filled_at) FROM xenon.futu_trades "
            f"WHERE {_scope_filter()} AND ticker=:ticker "
            f"  AND realized_pnl IS NOT NULL"
        ), {**_scope_params(scope), "ticker": ticker}).scalar()


# ---- cash flow -----------------------------------------------------------

_INSERT_CASH_FLOW = text("""
    INSERT INTO xenon.futu_cash_flow (
        futu_flow_id, flow_type, futu_type_raw, amount, amount_original,
        original_currency, fx_rate_used, ticker, description, flow_at, raw,
        broker, account_env, broker_account)
    VALUES (:futu_flow_id, :flow_type, :futu_type_raw, :amount,
            :amount_original, :original_currency, :fx_rate_used, :ticker,
            :description, :flow_at, CAST(:raw AS jsonb),
            :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, futu_flow_id)
    DO NOTHING
""")


def insert_cash_flows(scope: AccountScope, rows: Iterable[dict]) -> int:
    import json
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(_INSERT_CASH_FLOW, {
                **_scope_params(scope),
                **{k: r.get(k) for k in (
                    "futu_flow_id", "flow_type", "futu_type_raw", "amount",
                    "amount_original", "original_currency", "fx_rate_used",
                    "ticker", "description", "flow_at")},
                "raw": json.dumps(r.get("raw", {}), default=str),
            })
            inserted += res.rowcount
    return inserted


def fetch_cash_flows(scope: AccountScope, start: date, end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(text(f"""
            SELECT futu_flow_id, flow_type, futu_type_raw, amount,
                   amount_original, original_currency, fx_rate_used,
                   ticker, description, flow_at, raw
              FROM xenon.futu_cash_flow
             WHERE {_scope_filter()}
               AND (flow_at AT TIME ZONE 'America/New_York')::date
                   BETWEEN :start AND :end
             ORDER BY flow_at, futu_flow_id
        """), {**_scope_params(scope), "start": start, "end": end}):
            yield dict(row._mapping)


def cursor_for_cash_flows(scope: AccountScope) -> date | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT MAX((flow_at AT TIME ZONE 'America/New_York')::date) "
            f"FROM xenon.futu_cash_flow WHERE {_scope_filter()}"
        ), _scope_params(scope)).scalar()


# ---- corporate actions ---------------------------------------------------

_INSERT_CORP = text("""
    INSERT INTO xenon.corporate_actions (
        ticker, action_type, effective_date, ratio_numerator,
        ratio_denominator, new_ticker, source, notes)
    VALUES (:ticker, :action_type, :effective_date, :ratio_numerator,
            :ratio_denominator, :new_ticker, 'FUTU_GET_REHAB',
            CAST(:notes AS jsonb))
    ON CONFLICT (ticker, action_type, effective_date) DO NOTHING
""")


def insert_corporate_actions(rows: Iterable[dict]) -> int:
    import json
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(_INSERT_CORP, {
                "ticker": r["ticker"], "action_type": r["action_type"],
                "effective_date": r["effective_date"],
                "ratio_numerator": r.get("ratio_numerator"),
                "ratio_denominator": r.get("ratio_denominator"),
                "new_ticker": r.get("new_ticker"),
                "notes": json.dumps(r.get("notes", {}), default=str),
            })
            inserted += res.rowcount
    return inserted


def cursor_for_corp_actions(scope: AccountScope) -> date | None:
    """Max effective_date across tickers ever held by this scope."""
    with get_sync_engine().connect() as conn:
        return conn.execute(text(f"""
            SELECT MAX(ca.effective_date) FROM xenon.corporate_actions ca
             WHERE ca.ticker IN (
               SELECT DISTINCT ticker FROM xenon.futu_trades
                WHERE {_scope_filter()})
        """), _scope_params(scope)).scalar()


def fetch_corp_actions_for_tickers(tickers: list[str]) -> Iterator[dict]:
    if not tickers:
        return
    with get_sync_engine().connect() as conn:
        for row in conn.execute(text("""
            SELECT ticker, action_type, effective_date,
                   ratio_numerator, ratio_denominator, new_ticker
              FROM xenon.corporate_actions
             WHERE ticker = ANY(:tickers)
             ORDER BY ticker, effective_date
        """), {"tickers": tickers}):
            yield dict(row._mapping)


# ---- nav_history (Futu rows only) ---------------------------------------

_WRITE_NAV = text("""
    INSERT INTO xenon.nav_history (
        broker, account_env, broker_account, date, nav, source,
        total, cash, stock_value, options_value)
    VALUES (:broker, :env, :acc, :date, :nav, :source,
            :total, :cash, :stock_value, 0)
    ON CONFLICT (broker, account_env, broker_account, date) DO UPDATE
       SET nav         = EXCLUDED.nav,
           source      = EXCLUDED.source,
           total       = EXCLUDED.total,
           cash        = EXCLUDED.cash,
           stock_value = EXCLUDED.stock_value
     WHERE xenon.nav_history.date = EXCLUDED.date
       AND xenon.nav_history.date = :upsert_today
""")


def write_nav_row(scope: AccountScope, recon_date: date, nav: Decimal,
                  source: str, cash: Decimal, stock_value: Decimal,
                  is_today: bool = False) -> None:
    """Insert a NAV row. UPSERT only if recon_date == is_today flag.

    Historical rows (source='reconstructed') are written once; the
    `:upsert_today` guard in the SQL prevents accidental mutation of
    past dates during daily syncs.
    """
    with get_sync_engine().begin() as conn:
        conn.execute(_WRITE_NAV, {
            **_scope_params(scope), "date": recon_date, "nav": nav,
            "source": source, "total": nav, "cash": cash,
            "stock_value": stock_value,
            "upsert_today": recon_date if is_today else date(1900, 1, 1),
        })


def fetch_nav_history(scope: AccountScope, start: date,
                      end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(text(f"""
            SELECT date, nav, source, total, cash, stock_value
              FROM xenon.nav_history
             WHERE {_scope_filter()}
               AND date BETWEEN :start AND :end
             ORDER BY date
        """), {**_scope_params(scope), "start": start, "end": end}):
            yield dict(row._mapping)


# ---- reconciliation log --------------------------------------------------

_WRITE_RECON_DAILY = text("""
    INSERT INTO xenon.futu_reconciliation_log (
        kind, recon_date, reported_nav, reconstructed_nav, delta,
        threshold, exceeded, notes, broker, account_env, broker_account)
    VALUES ('daily', :recon_date, :reported, :reconstructed, :delta,
            :threshold, :exceeded, CAST(:notes AS jsonb),
            :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, recon_date)
    WHERE kind='daily'
    DO UPDATE SET
        reported_nav      = EXCLUDED.reported_nav,
        reconstructed_nav = EXCLUDED.reconstructed_nav,
        delta             = EXCLUDED.delta,
        threshold         = EXCLUDED.threshold,
        exceeded          = EXCLUDED.exceeded,
        notes             = EXCLUDED.notes,
        computed_at       = now()
""")


def write_recon_daily(scope: AccountScope, recon_date: date,
                      reported: Decimal | None,
                      reconstructed: Decimal | None,
                      threshold: Decimal,
                      notes: dict | None = None) -> None:
    import json
    delta = (reported - reconstructed) if (reported is not None
                                           and reconstructed is not None) else None
    exceeded = (delta is not None and abs(delta) > threshold)
    with get_sync_engine().begin() as conn:
        conn.execute(_WRITE_RECON_DAILY, {
            **_scope_params(scope), "recon_date": recon_date,
            "reported": reported, "reconstructed": reconstructed,
            "delta": delta, "threshold": threshold, "exceeded": exceeded,
            "notes": json.dumps(notes or {}, default=str),
        })


_WRITE_RECON_ANOMALY = text("""
    INSERT INTO xenon.futu_reconciliation_log (
        kind, recon_date, notes, broker, account_env, broker_account)
    VALUES ('anomaly', :recon_date, CAST(:notes AS jsonb),
            :broker, :env, :acc)
""")


def write_recon_anomaly(scope: AccountScope, recon_date: date, notes: dict) -> None:
    import json
    with get_sync_engine().begin() as conn:
        conn.execute(_WRITE_RECON_ANOMALY, {
            **_scope_params(scope), "recon_date": recon_date,
            "notes": json.dumps(notes, default=str),
        })
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_history_queries.py -xvs
```

Expected: 6 pass.

- [ ] **Step 5: Stage**

```bash
git add src/xenon/db/queries/futu_history.py \
        scripts/tests/test_futu_history_queries.py \
        scripts/tests/conftest.py
```

Stop.

---

## Milestone 5 — Sync orchestration (Tasks 10-14)

### Task 10: Sync service skeleton — advisory lock, request validation, cursors, no-op path

**Files:**

- Create: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_futu_sync_request_validation.py`
- Test: `scripts/tests/test_futu_sync_advisory_lock.py`

- [ ] **Step 1: Write the tests**

```python
# scripts/tests/test_futu_sync_request_validation.py
"""POST /futu/history/sync rejects bad `since` values with structured errors."""
import pytest
from datetime import date
from xenon.api.services.futu_history import (
    parse_since, SinceUnparseable, SinceOutOfRange, MIN_HISTORICAL_DATE,
)


def test_auto_returns_none():
    assert parse_since("auto") is None


def test_iso_date_in_range():
    assert parse_since("2024-06-15") == date(2024, 6, 15)


def test_before_floor_rejected():
    with pytest.raises(SinceOutOfRange) as exc:
        parse_since("2020-01-01")
    assert exc.value.reason_code == "since_out_of_range"


def test_future_date_rejected():
    with pytest.raises(SinceOutOfRange):
        parse_since("2050-01-01")


def test_garbage_rejected():
    with pytest.raises(SinceUnparseable):
        parse_since("yesterday")


def test_floor_constant_is_may_first_2024():
    assert MIN_HISTORICAL_DATE == date(2024, 5, 1)
```

```python
# scripts/tests/test_futu_sync_advisory_lock.py
"""Singleflight: concurrent sync calls log and exit; only one runs."""
import threading
from unittest.mock import MagicMock, patch
from xenon.api.services.futu_history import futu_history_sync
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_concurrent_invocations_singleflight(clean_scope):
    started = threading.Event()
    release = threading.Event()

    def fake_run(*args, **kwargs):
        started.set()
        release.wait(timeout=5.0)
        return {"fills_added": 0, "cash_flows_added": 0,
                "nav_rows_added": 0, "warnings": []}

    with patch("xenon.api.services.futu_history._run_sync_under_lock",
               side_effect=fake_run):
        t1 = threading.Thread(target=futu_history_sync,
                              kwargs={"scope": SCOPE, "since": None})
        t1.start()
        started.wait(timeout=5.0)

        # Second call must short-circuit with "another sync in progress".
        result2 = futu_history_sync(scope=SCOPE, since=None)
        assert result2["status"] == "in_progress"
        release.set()
        t1.join()
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_sync_request_validation.py \
              scripts/tests/test_futu_sync_advisory_lock.py -xvs
```

- [ ] **Step 3: Implement the service skeleton**

```python
# src/xenon/api/services/futu_history.py
"""Futu-history sync orchestrator.

One idempotent function — futu_history_sync(scope, since) — drives both
first-run backfill and daily-forward UPSERT. Invocation paths:
  - CLI: scripts/futu_history_sync.py
  - FastAPI: POST /futu/history/sync
  - launchd: 16:30 ET daily (runs CLI)
Singleflight via pg_try_advisory_lock(7343001).
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from xenon.api.services.advisory_lock import (
    LOCK_KEY_FUTU_HISTORY_SYNC, try_advisory_lock,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    cursor_for_fills,
    cursor_for_cash_flows,
    cursor_for_corp_actions,
)
from xenon.db.engine import get_sync_engine

log = logging.getLogger(__name__)

MIN_HISTORICAL_DATE = date(2024, 5, 1)


@dataclass
class SinceUnparseable(Exception):
    reason_code: str = "since_unparseable"
    bad_value: str = ""


@dataclass
class SinceOutOfRange(Exception):
    reason_code: str = "since_out_of_range"
    bad_value: str = ""


def parse_since(since: str | None) -> date | None:
    """Returns the start date or None for 'auto'. Raises structured errors.

    Upper bound uses America/New_York date — matches every other date-boundary
    check in the plan. UTC would let `since=tomorrow_ET` slip through between
    19:00 ET (= 00:00 UTC next day) and 04:00 ET on the boundary day.
    """
    from zoneinfo import ZoneInfo

    if since is None or since == "auto":
        return None
    try:
        d = date.fromisoformat(since)
    except ValueError:
        raise SinceUnparseable(bad_value=since)
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if d < MIN_HISTORICAL_DATE or d > today_et:
        raise SinceOutOfRange(bad_value=since)
    return d


def futu_history_sync(scope: AccountScope, since: date | None,
                      rematch: bool = False,
                      until_date: date | None = None) -> dict[str, Any]:
    """Idempotent orchestrator. Returns a JSON-serializable result dict."""
    eng = get_sync_engine()
    with eng.connect() as conn:
        with try_advisory_lock(conn, LOCK_KEY_FUTU_HISTORY_SYNC) as locked:
            if not locked:
                log.info("futu_history_sync: another run holds the lock; skipping")
                return {"status": "in_progress",
                        "fills_added": 0, "cash_flows_added": 0,
                        "nav_rows_added": 0, "warnings": []}
            return _run_sync_under_lock(scope=scope, since=since,
                                        rematch=rematch,
                                        until_date=until_date)


def _run_sync_under_lock(scope: AccountScope, since: date | None,
                         rematch: bool,
                         until_date: date | None) -> dict[str, Any]:
    """Placeholder for the body — filled out in Tasks 11-14."""
    log.info("futu_history_sync placeholder run scope=%s since=%s", scope, since)
    return {"status": "ok", "fills_added": 0, "cash_flows_added": 0,
            "nav_rows_added": 0, "warnings": []}
```

Add the `try_advisory_lock` helper to `src/xenon/api/services/advisory_lock.py` if it does not already exist (Read the existing file first; mirror the pattern of `LOCK_KEY_VCG_CRI`):

```python
@contextlib.contextmanager
def try_advisory_lock(conn, key: int):
    """Wrap unlock in try/except so a stale connection's unlock error
    doesn't mask the original exception. Postgres releases session locks
    when the session ends anyway — this is just for the in-flight path."""
    locked = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                          {"k": key}).scalar()
    try:
        yield bool(locked)
    finally:
        if locked:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                             {"k": key})
            except Exception:
                logging.getLogger(__name__).warning(
                    "advisory_unlock(%d) failed; relying on session-close "
                    "release", key, exc_info=True)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_sync_request_validation.py \
              scripts/tests/test_futu_sync_advisory_lock.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/api/services/futu_history.py \
        src/xenon/api/services/advisory_lock.py \
        scripts/tests/test_futu_sync_request_validation.py \
        scripts/tests/test_futu_sync_advisory_lock.py
```

Stop.

---

### Task 11: Sync steps 2-3 — pull fills + cash flow, idempotency, late-arrival flag, fee dedup at query time

**Files:**

- Modify: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_futu_sync_idempotent.py`
- Test: `scripts/tests/test_futu_sync_resumable.py`
- Test: `scripts/tests/test_futu_fee_dedup_order_independence.py`
- Test: `scripts/tests/test_futu_trade_date_tz_partition.py`
- Test: `scripts/tests/test_futu_sync_late_arrival_rematch.py`
- Test: `scripts/tests/test_futu_fx_missing.py`
- Test: `scripts/tests/test_fx_backfill_called_before_cash_flow.py` (C27)

- [ ] **Step 1: Write the idempotency test**

```python
# scripts/tests/test_futu_sync_idempotent.py
"""Running sync twice with the same inputs writes each row exactly once."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd

from xenon.api.services.futu_history import futu_history_sync
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    fetch_fills,
    fetch_cash_flows,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _fake_deals_df():
    return (0, pd.DataFrame([{
        "deal_id": "d1", "order_id": "o1", "code": "US.AAPL",
        "trd_side": "BUY", "qty": 100, "price": 150.0,
        "create_time": "2024-05-01 10:00:00", "currency": "USD",
        "fee": 1.0,
    }]))


def _fake_cash_df():
    return (0, pd.DataFrame([{
        "flow_id": "f1", "cashflow_type": "FUND_IN", "amount": 1000.0,
        "currency": "USD", "remark": "wire", "cashflow_remark": "",
        "create_time": "2024-05-01 09:00:00",
    }]))


def test_double_sync_is_a_noop(clean_scope, monkeypatch):
    trade_ctx = MagicMock()
    trade_ctx.history_deal_list_query.return_value = _fake_deals_df()
    trade_ctx.get_acc_cash_flow.return_value = _fake_cash_df()
    trade_ctx.accinfo_query.return_value = (0, pd.DataFrame([{
        "total_assets": 11000, "cash": 10000, "unrealized_pl": 0,
    }]))
    trade_ctx.position_list_query.return_value = (0, pd.DataFrame([{
        "code": "US.AAPL", "qty": 100, "cost_price": 150.0,
    }]))

    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())

    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_trade_ctx",
        lambda: trade_ctx)
    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_quote_ctx",
        lambda: quote_ctx)

    r1 = futu_history_sync(scope=SCOPE, since=date(2024, 5, 1),
                           until_date=date(2024, 5, 1))
    r2 = futu_history_sync(scope=SCOPE, since=date(2024, 5, 1),
                           until_date=date(2024, 5, 1))
    assert r1["fills_added"] == 1
    assert r2["fills_added"] == 0
    assert r1["cash_flows_added"] == 1
    assert r2["cash_flows_added"] == 0
    assert len(list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))) == 1
```

- [ ] **Step 2: Write the resumable test**

```python
# scripts/tests/test_futu_sync_resumable.py
"""A failure mid-batch leaves partial state that the next run cleans up.

No single wrapping transaction — each ON CONFLICT DO NOTHING runs in its own
commit, so partial writes are durable + idempotent on retry."""
from datetime import date
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from xenon.api.services.futu_history import futu_history_sync
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import fetch_fills

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_crash_after_fills_resumes_cleanly(clean_scope, monkeypatch):
    trade_ctx = MagicMock()
    trade_ctx.history_deal_list_query.return_value = (0, pd.DataFrame([
        {"deal_id": "d1", "order_id": "o", "code": "US.AAPL",
         "trd_side": "BUY", "qty": 1, "price": 1.0,
         "create_time": "2024-05-01 10:00:00", "currency": "USD"},
    ]))
    # Cash flow step raises mid-run on first invocation.
    trade_ctx.get_acc_cash_flow.side_effect = [
        RuntimeError("simulated OpenD disconnect"),
        (0, pd.DataFrame([])),
    ]
    trade_ctx.accinfo_query.return_value = (0, pd.DataFrame([{
        "total_assets": 1, "cash": 1, "unrealized_pl": 0}]))
    trade_ctx.position_list_query.return_value = (0, pd.DataFrame([{
        "code": "US.AAPL", "qty": 1, "cost_price": 1.0}]))

    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_trade_ctx",
        lambda: trade_ctx)
    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_quote_ctx",
        lambda: MagicMock(get_rehab=lambda c: (0, pd.DataFrame())))

    with pytest.raises(RuntimeError):
        futu_history_sync(scope=SCOPE, since=date(2024, 5, 1),
                          until_date=date(2024, 5, 1))
    # The fill from step 2 should be durable despite the step-3 crash.
    assert len(list(fetch_fills(SCOPE,
                                date(2024, 5, 1), date(2024, 5, 1)))) == 1

    # Second run finishes cleanly without duplicating the fill.
    r = futu_history_sync(scope=SCOPE, since=date(2024, 5, 1),
                          until_date=date(2024, 5, 1))
    assert r["fills_added"] == 0
```

- [ ] **Step 3: Write the fee-dedup test**

```python
# scripts/tests/test_futu_fee_dedup_order_independence.py
"""A fee event that references a deal_id is dropped at *query time*, not
ingest time. Order-of-arrival irrelevant."""
from datetime import date, datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_history import sum_cash_flow_for_date
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_fills,
    insert_cash_flows,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")
D = date(2024, 5, 1)


def _fill(deal_id):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action="BUY", quantity=1,
        price=Decimal("100"), fees=Decimal("1"),
        filled_at=datetime.fromisoformat("2024-05-01T10:00:00+00:00"),
        raw={},
    )


def _fee_cf(flow_id, deal_id):
    return dict(
        futu_flow_id=flow_id, flow_type="fee", futu_type_raw="COMMISSION",
        amount=Decimal("-1"), amount_original=Decimal("-1"),
        original_currency="USD", fx_rate_used=None, ticker="AAPL",
        description="commission", flow_at=datetime.fromisoformat(
            "2024-05-01T10:00:00+00:00"),
        raw={"deal_id": deal_id}, notes={"deal_id": deal_id},
    )


def test_fee_then_deal(clean_scope):
    insert_cash_flows(SCOPE, [_fee_cf("f1", "d1")])
    insert_fills(SCOPE, [_fill("d1")])
    # Cash-flow sum for the date should EXCLUDE the fee row because deal_id
    # 'd1' now exists as a fill (whose .fees column already accounts for it).
    assert sum_cash_flow_for_date(SCOPE, D) == Decimal("0")


def test_deal_then_fee(clean_scope):
    insert_fills(SCOPE, [_fill("d1")])
    insert_cash_flows(SCOPE, [_fee_cf("f1", "d1")])
    assert sum_cash_flow_for_date(SCOPE, D) == Decimal("0")
```

- [ ] **Step 4: Write the trade-date TZ-partition test**

```python
# scripts/tests/test_futu_trade_date_tz_partition.py
"""A 21:30 ET fill (= 01:30 UTC next day) buckets to the ET trade date,
even when the Postgres session runs in UTC."""
from datetime import date, datetime, timezone
from decimal import Decimal
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_fills,
    fetch_fills,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_2130_et_fill_buckets_to_et_date(clean_scope):
    # 2024-05-01 21:30 ET == 2024-05-02 01:30 UTC.
    fill = dict(
        futu_deal_id="d1", futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action="BUY", quantity=1,
        price=Decimal("100"), fees=Decimal("0"),
        filled_at=datetime(2024, 5, 2, 1, 30, tzinfo=timezone.utc),
        raw={},
    )
    insert_fills(SCOPE, [fill])
    # Query by ET trade date 2024-05-01 must include this row.
    rows = list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert [r["futu_deal_id"] for r in rows] == ["d1"]
```

- [ ] **Step 5: Write the late-arrival rematch test**

```python
# scripts/tests/test_futu_sync_late_arrival_rematch.py
"""A backfilled older BUY after later SELLs computed realized_pnl triggers
rematch on next sync — no operator action."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
import pandas as pd

from xenon.api.services.futu_history import (
    detect_tickers_needing_rematch,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_fills,
    update_realized_pnl,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _row(deal_id, action, qty, price, t):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action=action, quantity=qty,
        price=Decimal(str(price)), fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t), raw={},
    )


def test_late_buy_triggers_rematch_signal(clean_scope):
    # Forward-FIFO ran: BUY then SELL on 5/02, realized_pnl recorded.
    insert_fills(SCOPE, [
        _row("d1", "BUY", 100, 150, "2024-05-02T10:00:00+00:00"),
        _row("d2", "SELL", 100, 160, "2024-05-02T11:00:00+00:00"),
    ])
    update_realized_pnl(SCOPE, "d2", Decimal("1000"))

    # Late-arriving BUY backfilled on 5/01.
    insert_fills(SCOPE, [
        _row("d0", "BUY", 50, 100, "2024-05-01T09:00:00+00:00"),
    ])

    tickers = detect_tickers_needing_rematch(SCOPE)
    assert "AAPL" in tickers
```

- [ ] **Step 6: Write the missing-FX test**

```python
# scripts/tests/test_futu_fx_missing.py
"""A cash flow with no fx_daily row for that date is stored with amount=NULL
+ an anomaly row to the reconciliation log."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
import pandas as pd

from xenon.api.services.futu_history import (
    convert_cash_flow_amount,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import fetch_cash_flows

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_missing_fx_writes_null_amount_and_anomaly(clean_scope):
    raw_row = {
        "futu_flow_id": "f1", "futu_type_raw": "DIVIDEND",
        "amount_original": Decimal("780"), "original_currency": "HKD",
        "flow_at": datetime(2024, 5, 1, 10, tzinfo=timezone.utc),
        "ticker": "00700", "description": "div",
        "raw": {},
    }
    # No fx_daily row for HKD on 2024-05-01 → returns (None, None) +
    # writes anomaly.
    amount, fx_rate = convert_cash_flow_amount(SCOPE, raw_row)
    assert amount is None
    assert fx_rate is None
```

- [ ] **Step 7: Run all six tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_sync_idempotent.py \
              scripts/tests/test_futu_sync_resumable.py \
              scripts/tests/test_futu_fee_dedup_order_independence.py \
              scripts/tests/test_futu_trade_date_tz_partition.py \
              scripts/tests/test_futu_sync_late_arrival_rematch.py \
              scripts/tests/test_futu_fx_missing.py -xvs
```

Expected: ImportError or AttributeError on the new functions.

- [ ] **Step 8: Implement steps 2-3 + the helpers**

Append to `src/xenon/api/services/futu_history.py`:

```python
from sqlalchemy import text
from decimal import Decimal

from xenon.clients.futu_history_fetcher import FutuHistoryFetcher
from xenon.clients.futu_quote_client import FutuQuoteClient
from xenon.utils.futu_fx import convert_to_usd, FxRateMissing
from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import (
    insert_fills,
    insert_cash_flows,
    insert_corporate_actions,
    fetch_fills,
    fetch_cash_flows,
    cursor_for_fills,
    cursor_for_cash_flows,
    cursor_for_corp_actions,
    write_recon_anomaly,
    newest_realized_pnl_at,
    null_realized_for_ticker,
)


# Default Futu cash-flow taxonomy mapping. Verify enum names empirically.
_FLOW_TAXONOMY = {
    "FUND_IN":      "deposit",
    "FUND_OUT":     "withdraw",
    "DIVIDEND":     "dividend",
    "INTEREST":     "interest",
    "COMMISSION":   "fee",
    "PLATFORM_FEE": "fee",
}


def _normalize_flow_type(raw: str) -> str:
    return _FLOW_TAXONOMY.get(raw.upper(), "other")


def _build_trade_ctx():  # pragma: no cover — overridden in tests
    from futu import OpenSecTradeContext, TrdMarket
    import os
    return OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )


def _build_quote_ctx():  # pragma: no cover
    from futu import OpenQuoteContext
    import os
    return OpenQuoteContext(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )


def convert_cash_flow_amount(scope: AccountScope, row: dict) -> tuple[Decimal | None,
                                                                Decimal | None]:
    """Return (amount_usd, fx_rate_used). Both None on missing FX."""
    cur = row["original_currency"]
    if cur == "USD":
        return row["amount_original"], None
    flow_date = row["flow_at"].date()
    try:
        amount = convert_to_usd(row["amount_original"], cur, flow_date)
    except FxRateMissing:
        write_recon_anomaly(scope, flow_date, notes={
            "anomaly_type": "missing_fx", "currency": cur,
            "flow_id": row["futu_flow_id"]})
        return None, None
    # The actual rate used = amount_original / amount. Guard against the
    # pathological zero-amount case so a malformed flow doesn't blow up the
    # whole pull with DivisionByZero.
    if amount and amount != 0:
        fx_rate = (row["amount_original"] / amount).quantize(Decimal("0.000001"))
    else:
        fx_rate = None
    return amount, fx_rate


def _pull_fills(scope: AccountScope, trade_ctx, start: date,
                end: date) -> int:
    fetcher = FutuHistoryFetcher(trade_ctx=trade_ctx,
                                 trd_env="REAL")
    rows = []
    for fill in fetcher.fetch_deals(start, end, acc_id=scope.broker_account):
        rows.append({
            "futu_deal_id": fill.futu_deal_id,
            "futu_order_id": fill.futu_order_id,
            "ticker": fill.ticker, "futu_code": fill.futu_code,
            "market": fill.market, "action": fill.action,
            "quantity": fill.quantity, "price": fill.price,
            "fees": fill.fees, "filled_at": fill.filled_at, "raw": fill.raw,
        })
    return insert_fills(scope, rows)


def _pull_cash_flow(scope: AccountScope, trade_ctx, start: date,
                    end: date) -> int:
    fetcher = FutuHistoryFetcher(trade_ctx=trade_ctx, trd_env="REAL")
    rows = []
    for cf in fetcher.fetch_cash_flow(start, end, acc_id=scope.broker_account):
        normalized = {
            "futu_flow_id": cf.futu_flow_id,
            "futu_type_raw": cf.futu_type_raw,
            "amount_original": cf.amount_original,
            "original_currency": cf.original_currency,
            "flow_at": cf.flow_at, "ticker": cf.ticker,
            "description": cf.description, "raw": cf.raw,
        }
        amount, fx_rate = convert_cash_flow_amount(scope, normalized)
        rows.append({
            **normalized,
            "amount": amount, "fx_rate_used": fx_rate,
            "flow_type": _normalize_flow_type(cf.futu_type_raw),
        })
    return insert_cash_flows(scope, rows)


def sum_cash_flow_for_date(scope: AccountScope, d: date) -> Decimal:
    """Sum cash flow amounts for an ET trade date, EXCLUDING fee rows whose
    notes.deal_id resolves to an existing futu_trades.futu_deal_id."""
    with get_sync_engine().connect() as conn:
        result = conn.execute(text(
            """
            SELECT COALESCE(SUM(cf.amount), 0)
              FROM xenon.futu_cash_flow cf
             WHERE cf.broker=:broker AND cf.account_env=:env
               AND cf.broker_account=:acc
               AND (cf.flow_at AT TIME ZONE 'America/New_York')::date = :d
               AND NOT EXISTS (
                   SELECT 1 FROM xenon.futu_trades t
                    WHERE t.broker=cf.broker
                      AND t.account_env=cf.account_env
                      AND t.broker_account=cf.broker_account
                      AND t.futu_deal_id = (cf.raw->>'deal_id'))
            """
        ), {"broker": scope.broker, "env": scope.account_env,
            "acc": scope.broker_account, "d": d}).scalar()
    return Decimal(str(result or 0))


def detect_tickers_needing_rematch(scope: AccountScope) -> set[str]:
    """A ticker needs rematch if any fill or corp action POSTDATES the
    newest non-null realized_pnl row for that ticker. Step 4 of the spec."""
    with get_sync_engine().connect() as conn:
        rows = conn.execute(text(
            f"""
            WITH per_ticker AS (
              SELECT ticker,
                     MAX(filled_at) FILTER (WHERE realized_pnl IS NOT NULL)
                                                    AS last_pnl_at,
                     MAX(filled_at)                 AS last_fill_at
                FROM xenon.futu_trades
               WHERE broker=:broker AND account_env=:env
                 AND broker_account=:acc
               GROUP BY ticker
            )
            SELECT ticker FROM per_ticker
             WHERE last_pnl_at IS NULL AND last_fill_at IS NOT NULL
                OR last_fill_at > last_pnl_at
            """
        ), {"broker": scope.broker, "env": scope.account_env,
            "acc": scope.broker_account}).fetchall()
    return {r[0] for r in rows}
```

Replace the body of `_run_sync_under_lock` with:

```python
def _run_sync_under_lock(scope: AccountScope, since: date | None,
                         rematch: bool,
                         until_date: date | None) -> dict[str, Any]:
    """Orchestrate the full sync under the advisory lock.

    Step order (spec § 5.3):
      1. compute [start, today] from cursors (with 1-day overlap)
      1.5. ensure FX coverage in fx_daily for [start, today] (C27)
      2. pull fills (deal_list_query)
      3. pull cash flow (acc_cash_flow) — needs FX from 1.5
      4. pull corporate actions (get_rehab) — moved before rematch per C28
      5. rematch tickers (FIFO) — consumes corp actions
      6. snapshot account state
      7. NAV backwalk (first run) or write_today_reported_nav (incremental)

    Contexts are explicitly closed in finally to avoid leaking TCP sockets to
    Futu OpenD across launchd invocations.
    """
    from xenon.utils.futu_fx import backfill_fx_series, FxBackfillFailed

    today = until_date or datetime.now(timezone.utc).date()
    cursor_fills = cursor_for_fills(scope)
    cursor_flows = cursor_for_cash_flows(scope)
    cursor_corp = cursor_for_corp_actions(scope)

    # Auto-cursor with 1-day overlap (C31). Filter `None` cursors so a
    # missing-corp-actions cursor doesn't silently force a full replay every
    # day. The `-1` overlap re-pulls the previous boundary to catch
    # same-window late arrivals around settlement.
    if since is not None:
        start = since
    else:
        cursors = [c for c in (cursor_fills, cursor_flows, cursor_corp)
                   if c is not None]
        if cursors:
            start = max(MIN_HISTORICAL_DATE, min(cursors) - timedelta(days=1))
        else:
            start = MIN_HISTORICAL_DATE

    trade_ctx = _build_trade_ctx()
    quote_ctx = _build_quote_ctx()
    warnings: list[str] = []
    try:
        # Step 1.5: ensure FX coverage for the window BEFORE pulling cash
        # flow. On a fresh DB this populates fx_daily for HKD/CNY across
        # [start, today] in one FRED hit. Idempotent: ON CONFLICT(currency,
        # date) DO NOTHING inside backfill_fx_series.
        try:
            for currency in ("HKD", "CNY"):
                backfill_fx_series(currency, start, today)
        except FxBackfillFailed as exc:
            return {"status": "error",
                    "reason_code": exc.reason_code,
                    "fills_added": 0, "cash_flows_added": 0,
                    "nav_rows_added": 0,
                    "warnings": [str(exc)]}

        # Step 2: fills
        fills_added = _pull_fills(scope, trade_ctx, start, today)
        # Step 3: cash flow (FX-aware)
        cash_flows_added = _pull_cash_flow(scope, trade_ctx, start, today)
        # NOTE: corp actions + rematch + snapshot + NAV are folded in by
        # Tasks 12, 13, 14. Their orchestration MUST: pull corp actions
        # BEFORE rematch (C28); --rematch wipes historical reconstructed
        # rows + _LOT_HISTORY_CACHE before walking (C25); first sync writes
        # the cost-basis anchor only as a recon-log row, not into
        # nav_history (C24).

        return {
            "status": "ok",
            "fills_added": fills_added,
            "cash_flows_added": cash_flows_added,
            "nav_rows_added": 0,   # filled in Task 14
            "warnings": warnings,
            "started_at": today.isoformat(),
            "completed_at": today.isoformat(),
        }
    finally:
        # C17: close OpenD contexts so launchd doesn't leak TCP sockets.
        try:
            trade_ctx.close()
        except Exception:
            log.warning("trade_ctx.close failed", exc_info=True)
        try:
            quote_ctx.close()
        except Exception:
            log.warning("quote_ctx.close failed", exc_info=True)
```

- [ ] **Step 9: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_sync_idempotent.py \
              scripts/tests/test_futu_sync_resumable.py \
              scripts/tests/test_futu_fee_dedup_order_independence.py \
              scripts/tests/test_futu_trade_date_tz_partition.py \
              scripts/tests/test_futu_sync_late_arrival_rematch.py \
              scripts/tests/test_futu_fx_missing.py -xvs
```

- [ ] **Step 10: Stage**

```bash
git add src/xenon/api/services/futu_history.py scripts/tests/test_futu_*.py
```

Stop.

---

### Task 12: Sync steps 4-5 — recompute realized P&L + snapshot account

**Files:**

- Modify: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_account_snapshots_futu_payload_shape.py`
- Test: `scripts/tests/test_futu_anomaly_log.py`

- [ ] **Step 1: Write the account-snapshot shape test**

```python
# scripts/tests/test_account_snapshots_futu_payload_shape.py
"""Step 5: snapshot_account_state writes the full fetch_portfolio() envelope
to account_snapshots.payload + mirrors net_liquidation into typed columns."""
import json
from datetime import date
from unittest.mock import MagicMock
import pandas as pd

from xenon.api.services.futu_history import snapshot_account_state
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from sqlalchemy import text

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_snapshot_writes_payload_and_typed_columns(clean_scope):
    trade_ctx = MagicMock()
    # C26: Futu SDK returns `total_assets`, not `net_liquidation`.
    trade_ctx.accinfo_query.return_value = (0, pd.DataFrame([{
        "total_assets": 12345.67, "cash": 1000, "unrealized_pl": 200,
    }]))
    trade_ctx.position_list_query.return_value = (0, pd.DataFrame([{
        "code": "US.AAPL", "qty": 10, "cost_price": 150.0,
        "nominal_price": 160.0, "market_val": 1600.0,
    }]))
    snapshot_account_state(SCOPE, trade_ctx)
    with get_sync_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT net_liquidation, bankroll, account, payload
              FROM xenon.account_snapshots
             WHERE broker='FUTU' AND account_env='paper'
               AND broker_account='pytest'
             ORDER BY id DESC LIMIT 1
        """)).first()
    # The xenon column net_liquidation = Futu's total_assets (canonical
    # cross-broker name).
    assert float(row.net_liquidation) == 12345.67
    assert float(row.bankroll) == 12345.67  # mirrored
    assert row.account == "pytest"  # C3: account column populated from broker_account
    payload = row.payload
    assert payload["account_summary"]["total_assets"] == 12345.67
    assert payload["positions"][0]["code"] == "US.AAPL"
    assert payload["positions"][0]["market_value"] == 1600.0
```

- [ ] **Step 2: Write the anomaly-log test**

```python
# scripts/tests/test_futu_anomaly_log.py
"""Anomaly rows can be written with NULL reported/reconstructed/delta and
several anomalies on the same date coexist."""
from datetime import date
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import write_recon_anomaly
from sqlalchemy import text

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_multiple_anomalies_same_date(clean_scope):
    write_recon_anomaly(SCOPE, date(2024, 5, 1),
                        notes={"anomaly_type": "missing_fx"})
    write_recon_anomaly(SCOPE, date(2024, 5, 1),
                        notes={"anomaly_type": "over_close"})
    with get_sync_engine().connect() as conn:
        n = conn.execute(text("""
            SELECT count(*) FROM xenon.futu_reconciliation_log
             WHERE kind='anomaly' AND recon_date=:d
               AND broker='FUTU' AND account_env='paper'
               AND broker_account='pytest'
        """), {"d": date(2024, 5, 1)}).scalar()
    assert n == 2
```

- [ ] **Step 3: Run tests to confirm failure**

```bash
uv run pytest scripts/tests/test_account_snapshots_futu_payload_shape.py \
              scripts/tests/test_futu_anomaly_log.py -xvs
```

- [ ] **Step 4: Implement step 4 (rematch) + step 5 (snapshot)**

Append to `src/xenon/api/services/futu_history.py`:

```python
from xenon.execution.futu_fifo_matcher import (
    match_fills, FillInput, CorpActionInput,
)
from xenon.db.queries.futu_history import (
    update_realized_pnl,
    fetch_corp_actions_for_tickers,
)


# C19: module-level cache of per-ticker FIFO lot history, used by the
# backward NAV walk in Task 14 to recover state when a ticker was closed
# and reopened (qty=0 mid-walk).
#
# Keyed by (broker, account_env, broker_account, ticker). Cleared whenever
# `--rematch` runs (C25) so a forced rematch sees fresh history.
_LOT_HISTORY_CACHE: dict[tuple[str, str, str, str], list[dict]] = {}


def _rematch_ticker(scope: AccountScope, ticker: str) -> None:
    """Re-run FIFO over the full history for this ticker, write realized_pnl
    back to each SELL row, AND populate _LOT_HISTORY_CACHE for the backward
    NAV walk to consume (spec § Failure modes: ticker-reopen)."""
    null_realized_for_ticker(scope, ticker)
    fills_raw = [r for r in fetch_fills(scope, MIN_HISTORICAL_DATE,
                                        date(2099, 12, 31))
                 if r["ticker"] == ticker]
    fills = [FillInput(
        futu_deal_id=r["futu_deal_id"], action=r["action"],
        quantity=int(r["quantity"]), price=Decimal(str(r["price"])),
        fees=Decimal(str(r["fees"])), filled_at=r["filled_at"],
    ) for r in fills_raw]
    corp = [CorpActionInput(
        id=str(ca.get("id", f"{ca['ticker']}-{ca['effective_date']}")),
        action_type=ca["action_type"],
        effective_date=ca["effective_date"],
        ratio_numerator=Decimal(str(ca["ratio_numerator"] or 1)),
        ratio_denominator=Decimal(str(ca["ratio_denominator"] or 1)),
    ) for ca in fetch_corp_actions_for_tickers([ticker])]
    # Request the side-channel lot_history — Task 14's backwalk consumes it.
    result = match_fills(fills, corp_actions=corp, return_lot_history=True)
    _LOT_HISTORY_CACHE[(scope.broker, scope.account_env,
                        scope.broker_account, ticker)] = result.lot_history
    for f in result.fills:
        if f.action == "SELL":
            update_realized_pnl(scope, f.futu_deal_id, f.realized_pnl)


def snapshot_account_state(scope: AccountScope, trade_ctx) -> None:
    """Read live accinfo + positions, write one row to xenon.account_snapshots.

    Futu's raw accinfo_query returns `total_assets` (not `net_liquidation` —
    C26). Reuse FutuClient where possible so this code path follows the
    same envelope-mapping as the live portfolio surface in server.py.
    """
    import json
    code, accinfo_df = trade_ctx.accinfo_query(
        acc_id=scope.broker_account, trd_env="REAL")
    if code != 0:
        raise RuntimeError(f"accinfo_query failed: {accinfo_df!r}")
    code, pos_df = trade_ctx.position_list_query(
        acc_id=scope.broker_account, trd_env="REAL")
    if code != 0:
        raise RuntimeError(f"position_list_query failed: {pos_df!r}")

    summary = accinfo_df.to_dict(orient="records")[0]
    # C26: Futu returns `total_assets` (raw SDK column); we map it to
    # `net_liquidation` for the xenon column. `net_liquidation` is the
    # canonical xenon name across both brokers; on the Futu side it equals
    # `total_assets`.
    raw_total = summary.get("total_assets")
    if raw_total is None:
        raise RuntimeError(
            "Futu accinfo_query returned no `total_assets` column — "
            "verify SDK version against C26 (run the Task 5 Step 0 probe)")
    nl = Decimal(str(raw_total))
    positions = []
    for r in pos_df.to_dict(orient="records"):
        positions.append({
            "code": r.get("code"),
            "qty": float(r.get("qty", 0)),
            "cost_price": float(r.get("cost_price", 0)),
            "nominal_price": float(r.get("nominal_price", 0)),
            "market_value": float(r.get("market_val", 0)),
        })
    payload = {"account_summary": summary, "positions": positions}

    with get_sync_engine().begin() as conn:
        prev_peak = conn.execute(text(
            f"""SELECT MAX(peak_value) FROM xenon.account_snapshots
                 WHERE {_scope_filter()}"""
        ), _scope_params(scope)).scalar() or Decimal("0")
        peak = max(Decimal(str(prev_peak)), nl)
        # C3: account_snapshots.account is NOT NULL with no default.
        # Bind it to scope.broker_account (same as broker_account column).
        conn.execute(text(
            """
            INSERT INTO xenon.account_snapshots (
                account, broker, account_env, broker_account, net_liquidation,
                bankroll, peak_value, payload, snapshot_at)
            VALUES (:acc, :broker, :env, :acc, :nl, :nl, :peak,
                    CAST(:payload AS jsonb), now())
            """
        ), {**_scope_params(scope), "nl": nl, "peak": peak,
            "payload": json.dumps(payload, default=str)})


def _scope_filter() -> str:  # local mirror — query module's _scope_filter
    return ("broker = :broker AND account_env = :env "
            "AND broker_account = :acc")


def _scope_params(scope: AccountScope) -> dict:
    return {"broker": scope.broker, "env": scope.account_env,
            "acc": scope.broker_account}
```

Update `_run_sync_under_lock` to call steps 4-6 in the spec order
(C28: corp actions BEFORE rematch). Snapshot follows rematch. NAV (step 7)
is folded in by Task 14.

```python
        # Step 4 prerequisite (C28): pull corp actions BEFORE rematch so
        # rematch sees split-aware lot accounting for any newly-pulled
        # corp action.
        pull_corporate_actions(scope, quote_ctx)         # implemented in T13

        # Step 4: recompute realized P&L for tickers needing rematch.
        # NOTE: --rematch wipes both historical reconstructed nav_history
        # rows AND _LOT_HISTORY_CACHE for the scope (see C25 in Task 14).
        if rematch:
            tickers = {row["ticker"] for row in
                       fetch_fills(scope, MIN_HISTORICAL_DATE,
                                   date(2099, 12, 31))}
        else:
            tickers = detect_tickers_needing_rematch(scope)
        for t in tickers:
            _rematch_ticker(scope, t)

        # Step 5: snapshot current account.
        snapshot_account_state(scope, trade_ctx)
```

C28: update `detect_tickers_needing_rematch` to also detect newly-pulled
corporate actions (so a freshly-pulled split forces a rematch):

```python
def detect_tickers_needing_rematch(scope: AccountScope) -> set[str]:
    with get_sync_engine().connect() as conn:
        rows = conn.execute(text(f"""
            WITH per_ticker AS (
              SELECT t.ticker,
                     MAX(t.filled_at) FILTER (WHERE realized_pnl IS NOT NULL)
                                                    AS last_pnl_at,
                     MAX(t.filled_at)               AS last_fill_at,
                     (SELECT MAX(ca.effective_date)
                        FROM xenon.corporate_actions ca
                       WHERE ca.ticker = t.ticker)  AS last_corp_at
                FROM xenon.futu_trades t
               WHERE {_scope_filter()}
               GROUP BY t.ticker
            )
            SELECT ticker FROM per_ticker
             WHERE last_pnl_at IS NULL AND last_fill_at IS NOT NULL
                OR last_fill_at > last_pnl_at
                OR (last_corp_at IS NOT NULL
                    AND last_corp_at >= (last_pnl_at::date))
        """), _scope_params(scope)).fetchall()
    return {r[0] for r in rows}
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_account_snapshots_futu_payload_shape.py \
              scripts/tests/test_futu_anomaly_log.py -xvs
```

- [ ] **Step 6: Stage**

```bash
git add src/xenon/api/services/futu_history.py \
        scripts/tests/test_account_snapshots_futu_payload_shape.py \
        scripts/tests/test_futu_anomaly_log.py
```

Stop.

---

### Task 13: Sync step 6 — pull corporate actions via `get_rehab`

**Files:**

- Modify: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_futu_sync_corp_action_pull.py`

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_sync_corp_action_pull.py
"""Step 6: pull corp actions for every ticker ever held; missing get_rehab
data triggers an anomaly + ticker is skipped without raising."""
from datetime import date
from unittest.mock import MagicMock
import pandas as pd

from xenon.api.services.futu_history import pull_corporate_actions
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import insert_fills
from decimal import Decimal
from datetime import datetime

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_pull_corp_actions_writes_split_row(clean_scope):
    # Seed a fill so NVDA is in the ticker universe.
    insert_fills(SCOPE, [dict(
        futu_deal_id="d1", futu_order_id="o", ticker="NVDA",
        futu_code="US.NVDA", market="US", action="BUY", quantity=1,
        price=Decimal("1000"), fees=Decimal("0"),
        filled_at=datetime(2024, 5, 1, 10, 0), raw={})])
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame([{
        "ex_div_date": "2024-06-10", "split_ratio": 10.0,
        "join_ratio": 1.0, "company_act_flag": 0x04}]))
    n = pull_corporate_actions(SCOPE, quote_ctx)
    assert n == 1


def test_pull_corp_actions_skips_on_empty(clean_scope):
    insert_fills(SCOPE, [dict(
        futu_deal_id="d1", futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action="BUY", quantity=1,
        price=Decimal("1"), fees=Decimal("0"),
        filled_at=datetime(2024, 5, 1, 10, 0), raw={})])
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())
    assert pull_corporate_actions(SCOPE, quote_ctx) == 0
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_sync_corp_action_pull.py -xvs
```

- [ ] **Step 3: Implement**

Append to `src/xenon/api/services/futu_history.py`:

```python
def pull_corporate_actions(scope: AccountScope, quote_ctx) -> int:
    """For every ticker ever held in this scope, call get_rehab and persist."""
    from xenon.clients.futu_quote_client import FutuQuoteClient
    client = FutuQuoteClient(quote_ctx=quote_ctx)
    with get_sync_engine().connect() as conn:
        tickers = [r[0] for r in conn.execute(text(
            f"""SELECT DISTINCT ticker FROM xenon.futu_trades
                 WHERE {_scope_filter()}"""
        ), _scope_params(scope)).fetchall()]

    rows = []
    for ticker in tickers:
        try:
            actions = list(client.fetch_corporate_actions(f"US.{ticker}"))
        except Exception as exc:
            log.warning("get_rehab(%s) failed: %s", ticker, exc)
            write_recon_anomaly(scope, datetime.now(timezone.utc).date(),
                                notes={"anomaly_type": "missing_rehab",
                                       "ticker": ticker,
                                       "error": str(exc)})
            continue
        for ca in actions:
            rows.append({
                "ticker": ca.ticker, "action_type": ca.action_type,
                "effective_date": ca.effective_date,
                "ratio_numerator": ca.ratio_numerator,
                "ratio_denominator": ca.ratio_denominator,
                "new_ticker": ca.new_ticker,
                "notes": {"raw": ca.raw} if hasattr(ca, "raw") else {},
            })
    return insert_corporate_actions(rows)
```

Update `_run_sync_under_lock` to call step 6 immediately before NAV reconstruction (Task 14):

```python
    # Step 6: corporate actions.
    pull_corporate_actions(scope, quote_ctx)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_sync_corp_action_pull.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/api/services/futu_history.py \
        scripts/tests/test_futu_sync_corp_action_pull.py
```

Stop.

---

### Task 14: Sync step 7 — NAV backward walk + daily forward UPSERT

This is the **most subtle task in the plan**. Read the spec's "NAV reconstruction — backward walk from today's anchor" section twice before starting.

**Files:**

- Modify: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_futu_nav_backwalk.py`
- Test: `scripts/tests/test_futu_backwalk_anchor_consistency.py`
- Test: `scripts/tests/test_futu_backwalk_symbol_change.py`
- Test: `scripts/tests/test_futu_backwalk_corp_action_reversal.py`
- Test: `scripts/tests/test_futu_backwalk_ticker_reopen_missing_lot_history.py`
- Test: `scripts/tests/test_futu_nav_historical_immutable.py`
- Test: `scripts/tests/test_futu_nav_today_upsert.py`
- Test: `scripts/tests/test_futu_nav_continue_advances_state.py`
- Test: `scripts/tests/test_futu_nav_past_missing_is_reconstructed.py`
- Test: `scripts/tests/test_unapply_sell_preserves_avg_cost.py`
- Test: `scripts/tests/test_unapply_directions.py` (C18: pin BUY/SELL un-apply signs)
- Test: `scripts/tests/test_futu_rematch_recomputes_historical.py` (C25: --rematch lever)
- Test: `scripts/tests/test_futu_reconciliation_log.py`

- [ ] **Step 1: Write the golden backwalk tests**

```python
# scripts/tests/test_futu_nav_backwalk.py
"""Golden-day cases for the backward walk."""
from datetime import date, datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_history import (
    run_backward_walk, AnchorState,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_fills,
    insert_cash_flows,
    fetch_nav_history,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _fill(deal_id, action, ticker, qty, price, t):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker=ticker,
        futu_code=f"US.{ticker}", market="US", action=action, quantity=qty,
        price=Decimal(str(price)), fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t), raw={},
    )


def _cf(flow_id, amount, t, flow_type="deposit"):
    return dict(
        futu_flow_id=flow_id, flow_type=flow_type,
        futu_type_raw=flow_type.upper(),
        amount=Decimal(str(amount)), amount_original=Decimal(str(amount)),
        original_currency="USD", fx_rate_used=None, ticker=None,
        description="", flow_at=datetime.fromisoformat(t), raw={},
    )


def test_pure_cash_deposit_walks_to_zero(clean_scope):
    # Today: cash=$1000, no positions. Yesterday: deposit of $1000 → cash[D-1]=0.
    insert_cash_flows(SCOPE, [_cf("f1", 1000,
                                  "2024-05-02T09:00:00+00:00")])
    anchor = AnchorState(today=date(2024, 5, 2),
                         cash=Decimal("1000"),
                         positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1),
                                  date(2024, 5, 2)))
    assert rows[0]["date"] == date(2024, 5, 1)
    assert rows[0]["nav"] == Decimal("0")
    assert rows[0]["source"] == "reconstructed"
    assert rows[1]["nav"] == Decimal("1000")


def test_single_round_trip_returns_open_state(clean_scope):
    # Day D: BUY 100 @ 150 ($15,000 spent). Day D+1: SELL 100 @ 160.
    # Today (D+1) state: cash=$16,000 (incl realized), positions empty.
    # Walk back: D-1 cash=$15,000 (paid for buy)? No — anchor cash is END
    # of D+1, so un-applying SELL on D+1 → cash[D] = 16000 - 16000 = 0,
    # positions[D] = {AAPL: 100@150}. nav[D] = 0 + 100*150 = 15000.
    insert_fills(SCOPE, [
        _fill("d1", "BUY", "AAPL", 100, 150,
              "2024-05-01T10:00:00+00:00"),
        _fill("d2", "SELL", "AAPL", 100, 160,
              "2024-05-02T10:00:00+00:00"),
    ])
    anchor = AnchorState(today=date(2024, 5, 2),
                         cash=Decimal("16000"),
                         positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1),
                                  date(2024, 5, 2)))
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2024, 5, 2)]["nav"] == Decimal("16000")
    # D had 100 sh @ avg_cost 150 + 0 cash = 15000
    assert by_d[date(2024, 5, 1)]["nav"] == Decimal("15000")
```

- [ ] **Step 2: Write the corp-action reversal test**

```python
# scripts/tests/test_futu_backwalk_corp_action_reversal.py
"""A 10:1 split on D+1 means walk-back qty(D) = qty(D+1) / 10,
avg_cost(D) = avg_cost(D+1) * 10."""
from datetime import date, datetime
from decimal import Decimal

from xenon.api.services.futu_history import (
    run_backward_walk, AnchorState,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_corporate_actions,
    fetch_nav_history,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_split_reverses_on_walk_back(clean_scope):
    # Anchor (D+1, post-split): 100 sh @ avg_cost $10 + $0 cash → NAV = $1000.
    insert_corporate_actions([{
        "ticker": "NVDA", "action_type": "split",
        "effective_date": date(2024, 6, 10),
        "ratio_numerator": Decimal("10"),
        "ratio_denominator": Decimal("1"),
        "new_ticker": None, "notes": {}}])
    from xenon.api.services.futu_history import _PositionState
    anchor = AnchorState(today=date(2024, 6, 10),
                         cash=Decimal("0"),
                         positions={"NVDA": _PositionState(
                             qty=Decimal("100"), avg_cost=Decimal("10"))})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 6, 9))
    rows = {r["date"]: r for r in fetch_nav_history(
        SCOPE, date(2024, 6, 9), date(2024, 6, 10))}
    # D-1 (pre-split): qty = 10, avg_cost = $100 → still NAV $1000.
    assert rows[date(2024, 6, 9)]["nav"] == Decimal("1000")
```

- [ ] **Step 3: Write the ticker-reopen + lot-history test**

```python
# scripts/tests/test_futu_backwalk_ticker_reopen_missing_lot_history.py
"""When a SELL on D+1 fully closes a ticker that's empty in today's anchor,
the walk-back uses the FIFO lot_history side channel to recreate the lot.
If lot_history is missing, walk writes an anomaly + zeros avg_cost prior to
reopen, but realized_pnl from the forward FIFO pass remains correct."""
from datetime import date, datetime
from decimal import Decimal
from xenon.api.services.futu_history import (
    run_backward_walk, AnchorState,
)
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    insert_fills,
    fetch_nav_history,
)
from sqlalchemy import text

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _f(deal_id, action, ticker, qty, price, t):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker=ticker,
        futu_code=f"US.{ticker}", market="US", action=action, quantity=qty,
        price=Decimal(str(price)), fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t), raw={},
    )


def test_walk_back_uses_lot_history_or_writes_anomaly(clean_scope):
    insert_fills(SCOPE, [
        _f("d1", "BUY", "AAPL", 50, 100, "2024-05-01T10:00:00+00:00"),
        _f("d2", "SELL", "AAPL", 50, 110, "2024-05-02T10:00:00+00:00"),
    ])
    # Anchor: cash=$5,500 (sold AAPL), positions empty.
    anchor = AnchorState(today=date(2024, 5, 2),
                         cash=Decimal("5500"),
                         positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = {r["date"]: r for r in fetch_nav_history(
        SCOPE, date(2024, 5, 1), date(2024, 5, 2))}
    # D had 50 sh @ $100 + $0 cash → $5,000.
    assert rows[date(2024, 5, 1)]["nav"] == Decimal("5000")
```

- [ ] **Step 4: Write the historical-immutability test**

```python
# scripts/tests/test_futu_nav_historical_immutable.py
"""After backfill, daily syncs must NOT touch past dates. Only --rematch does."""
from datetime import date
from decimal import Decimal
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    write_nav_row,
    fetch_nav_history,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_write_nav_row_with_is_today_false_does_not_upsert(clean_scope):
    write_nav_row(SCOPE, recon_date=date(2024, 5, 1),
                  nav=Decimal("100"), source="reconstructed",
                  cash=Decimal("100"), stock_value=Decimal("0"))
    # Second attempt without is_today must NOT overwrite.
    write_nav_row(SCOPE, recon_date=date(2024, 5, 1),
                  nav=Decimal("999"), source="reported",
                  cash=Decimal("999"), stock_value=Decimal("0"))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert rows[0]["nav"] == Decimal("100")
    assert rows[0]["source"] == "reconstructed"


def test_is_today_true_does_upsert(clean_scope):
    today = date(2024, 5, 1)
    write_nav_row(SCOPE, recon_date=today, nav=Decimal("100"),
                  source="reported", cash=Decimal("100"),
                  stock_value=Decimal("0"), is_today=True)
    write_nav_row(SCOPE, recon_date=today, nav=Decimal("200"),
                  source="reported", cash=Decimal("200"),
                  stock_value=Decimal("0"), is_today=True)
    rows = list(fetch_nav_history(SCOPE, today, today))
    assert rows[0]["nav"] == Decimal("200")
```

- [ ] **Step 5: Write the today-UPSERT test**

```python
# scripts/tests/test_futu_nav_today_upsert.py
"""14:00 ET sync writes today's reported NAV; 16:30 ET sync overwrites it."""
from datetime import date
from decimal import Decimal
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    write_nav_row,
    fetch_nav_history,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_intraday_then_eod_overwrite(clean_scope):
    today = date(2024, 5, 1)
    write_nav_row(SCOPE, recon_date=today, nav=Decimal("10000"),
                  source="reported", cash=Decimal("5000"),
                  stock_value=Decimal("5000"), is_today=True)
    write_nav_row(SCOPE, recon_date=today, nav=Decimal("10100"),
                  source="reported", cash=Decimal("5000"),
                  stock_value=Decimal("5100"), is_today=True)
    rows = list(fetch_nav_history(SCOPE, today, today))
    assert rows[0]["nav"] == Decimal("10100")
```

- [ ] **Step 6: Write the partial-row state-advance test**

```python
# scripts/tests/test_futu_nav_continue_advances_state.py
"""Pre-seeded partial nav_history row in the middle of the window: sync
must NOT skip cash/position state advancement on that date."""
from datetime import date
from decimal import Decimal

from xenon.api.services.futu_history import (
    run_backward_walk, AnchorState,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    fetch_nav_history,
    write_nav_row,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_existing_row_does_not_break_downstream_state(clean_scope):
    # Pre-seed an unrelated nav_history row mid-window.
    write_nav_row(SCOPE, date(2024, 5, 1), Decimal("9999"),
                  "reconstructed", Decimal("9999"), Decimal("0"))
    anchor = AnchorState(today=date(2024, 5, 3),
                         cash=Decimal("1000"),
                         positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    # Walk should not have overwritten the pre-seeded 9999 row (immutable),
    # but state advancement at 5/01 must still be correct: 5/02 nav must
    # have been computed on top of advancing-through-5/01 state, not skipped.
    rows = {r["date"]: r for r in fetch_nav_history(
        SCOPE, date(2024, 5, 1), date(2024, 5, 3))}
    assert rows[date(2024, 5, 1)]["nav"] == Decimal("9999")  # immutable
    assert rows[date(2024, 5, 2)]["nav"] == Decimal("1000")  # not skipped
```

- [ ] **Step 7: Write the past-missing-is-reconstructed test**

```python
# scripts/tests/test_futu_nav_past_missing_is_reconstructed.py
from datetime import date, timedelta
from decimal import Decimal
from xenon.api.services.futu_history import (
    run_backward_walk, AnchorState,
)
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import fetch_nav_history

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_past_missing_rows_are_reconstructed(clean_scope):
    today = date(2024, 5, 4)
    anchor = AnchorState(today=today, cash=Decimal("0"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), today))
    # All historical rows reconstructed; today's row is written separately
    # by the daily-forward path (see write_today_reported_nav).
    for r in rows:
        if r["date"] < today:
            assert r["source"] == "reconstructed"
```

- [ ] **Step 8: Write the reconciliation log test**

```python
# scripts/tests/test_futu_reconciliation_log.py
from datetime import date
from decimal import Decimal
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import write_recon_daily
from sqlalchemy import text

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_threshold_scales_with_nav(clean_scope):
    # $50 delta on $10k NAV → 0.5% → exceeds $25 minimum.
    write_recon_daily(SCOPE, date(2024, 5, 1),
                      reported=Decimal("10050"),
                      reconstructed=Decimal("10000"),
                      threshold=max(Decimal("25"),
                                    Decimal("10050") * Decimal("0.001")))
    with get_sync_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT delta, threshold, exceeded "
            "FROM xenon.futu_reconciliation_log "
            "WHERE broker='FUTU' AND account_env='paper' "
            "  AND broker_account='pytest' AND kind='daily'"
        )).first()
    assert row.delta == Decimal("50")
    assert row.exceeded is True
```

- [ ] **Step 8b: Write the C18 un-apply direction regression test**

This test pins both directions of the BUY/SELL un-apply so a future
"simpler" refactor can't quietly flip the signs again.

```python
# scripts/tests/test_unapply_directions.py
"""Pin _unapply_buy + _unapply_sell signs against the forward FIFO."""
from decimal import Decimal
from xenon.api.services.futu_history import (
    _unapply_buy, _unapply_sell, _PositionState,
)


def test_unapply_buy_subtracts_qty():
    # END-OF-(D+1) state: long 100 sh @ avg_cost 150.
    # BUY on D+1: 10 sh @ 160. END-OF-D = 90 sh @ pre_total/90.
    post = _PositionState(qty=Decimal("100"), avg_cost=Decimal("150"))
    pre = _unapply_buy(post, qty=Decimal("10"), price=Decimal("160"),
                       fees=Decimal("1"))
    assert pre.qty == Decimal("90")
    # post total = 100*150 = 15000; minus 10*160 = 13400; / 90 ~= 148.8889.
    assert (pre.avg_cost - Decimal("148.8889")).copy_abs() < Decimal("0.001")


def test_unapply_sell_adds_qty_keeps_avg_cost():
    post = _PositionState(qty=Decimal("50"), avg_cost=Decimal("100"))
    pre = _unapply_sell(post, qty=Decimal("25"))
    assert pre.qty == Decimal("75")
    assert pre.avg_cost == Decimal("100")
```

- [ ] **Step 8c: Write the C25 --rematch regression test**

```python
# scripts/tests/test_futu_rematch_recomputes_historical.py
"""--rematch wipes historical reconstructed nav_history rows + the
lot_history cache for the scope, then rebuilds via run_backward_walk."""
from datetime import date
from decimal import Decimal
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import write_nav_row, fetch_nav_history

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_rematch_overwrites_wrong_historical_row(clean_scope, monkeypatch):
    """Pre-seed a wrong-value historical row, run sync without --rematch
    (row unchanged), run sync with --rematch (row corrected).

    The full sync orchestration requires mocked Futu contexts — see
    test_futu_sync_idempotent.py for the mocking shape; reuse it here
    and pass `rematch=True` on the second call.
    """
    # Step 1: pre-seed a deliberately wrong historical row.
    write_nav_row(SCOPE, date(2024, 5, 1), Decimal("9999"),
                  "reconstructed", Decimal("9999"), Decimal("0"))

    # … futu_history_sync(SCOPE, since=None, rematch=False) call here
    # … assert pre-seeded row unchanged (9999)

    # … futu_history_sync(SCOPE, since=None, rematch=True) call here
    # … assert row was deleted + rewritten to correct cost-basis NAV
    # (concrete numbers depend on the mocked Futu data).

    # Pin only the lever: rows = list(fetch_nav_history(SCOPE, 2024-05-01,
    # 2024-05-01)); assert rows[0]["nav"] != Decimal("9999") after rematch.
```

- [ ] **Step 9: Run all tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_nav_backwalk.py \
              scripts/tests/test_futu_backwalk_corp_action_reversal.py \
              scripts/tests/test_futu_backwalk_ticker_reopen_missing_lot_history.py \
              scripts/tests/test_futu_nav_historical_immutable.py \
              scripts/tests/test_futu_nav_today_upsert.py \
              scripts/tests/test_futu_nav_continue_advances_state.py \
              scripts/tests/test_futu_nav_past_missing_is_reconstructed.py \
              scripts/tests/test_unapply_directions.py \
              scripts/tests/test_futu_rematch_recomputes_historical.py \
              scripts/tests/test_futu_reconciliation_log.py -xvs
```

- [ ] **Step 10: Implement the backward walk + daily forward write**

Append to `src/xenon/api/services/futu_history.py`:

```python
from dataclasses import dataclass, field
from datetime import timedelta

from xenon.db.queries.futu_history import (
    write_nav_row,
    write_recon_daily,
    fetch_corp_actions_for_tickers,
)


@dataclass
class _PositionState:
    qty: Decimal
    avg_cost: Decimal


@dataclass
class AnchorState:
    """End-of-`today` state used as the walk anchor.

    `cash` excludes today's open positions' market value — it is just the
    raw cash balance. `positions` is a dict ticker → _PositionState with
    qty + avg_cost (cost basis). cost_basis_NAV = cash + Σ qty × avg_cost.
    """
    today: date
    cash: Decimal
    positions: dict[str, _PositionState] = field(default_factory=dict)

    def cost_basis_nav(self) -> Decimal:
        stock = sum((p.qty * p.avg_cost for p in self.positions.values()),
                    Decimal("0"))
        return self.cash + stock

    def stock_value(self) -> Decimal:
        return sum((p.qty * p.avg_cost for p in self.positions.values()),
                   Decimal("0"))


def _build_anchor_from_live_account(scope: AccountScope) -> AnchorState:
    """Build an AnchorState from the live Futu account snapshot.

    C26: Reuse `FutuClient.fetch_portfolio()` (the same lazy-init client
    FastAPI uses for live portfolio reads in `server._get_futu_client()`)
    so the raw SDK column mapping (`total_assets` → net_liquidation,
    `cost_price` → avg_cost) lives in one place. Direct SDK calls here
    would diverge.

    NOTE: this CLI/sync path runs in a subprocess (no FastAPI lifespan), so
    we instantiate a fresh `FutuClient` rather than reusing
    `server._get_futu_client()`. Construction mirrors server.py's lazy-init
    pattern (env-driven host/port/firm).
    """
    import os
    from xenon.clients.futu_client import FutuClient

    client = FutuClient(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
        security_firm=os.environ.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES"),
        trd_env=os.environ.get("FUTU_TRD_ENV", "REAL"),
        filter_trading_market=os.environ.get("FUTU_MARKET", "US"),
    )
    portfolio = client.fetch_portfolio()
    summary = portfolio["account_summary"]
    cash = Decimal(str(summary["cash"]))
    positions: dict[str, _PositionState] = {}
    for row in portfolio["positions"]:
        market, ticker = (row["code"].split(".", 1)
                          if "." in row["code"]
                          else ("", row["code"]))
        if market != "US":
            continue
        positions[ticker.upper()] = _PositionState(
            qty=Decimal(str(row["qty"])),
            avg_cost=Decimal(str(row["cost_price"])),
        )
    return AnchorState(today=datetime.now(timezone.utc).date(),
                       cash=cash, positions=positions)


def _unapply_buy(pos: _PositionState, qty: Decimal, price: Decimal,
                 fees: Decimal) -> _PositionState:
    """Reverse a forward BUY: end-of-D qty = end-of-(D+1) qty − Q.

    C18 (supersedes C7): the walk processes D = today-1, today-2 …
    un-applying trades that happened on next_day = D+1. So `pos` arrives as
    END-OF-next_day state and we compute END-OF-D state (BEFORE next_day's
    fills happened).

    A BUY on next_day ADDS quantity going forward, so the inverse
    (going backward) SUBTRACTS: pre_qty = post_qty − qty.

    Forward FIFO adds Q shares at avg_cost=price (fees tracked separately,
    NOT in cost basis), so total_cost_basis goes up by price*Q. Reverse:
    pre_total = post_total − price*Q. The `fees` parameter is kept for
    symmetry with the caller but unused — cash side already accounts for
    fees: state_cash += price*qty + fees correctly mirrors the forward BUY
    cash drain.
    """
    pre_qty = pos.qty - qty
    if pre_qty <= 0:
        return _PositionState(qty=Decimal("0"), avg_cost=Decimal("0"))
    post_total = pos.qty * pos.avg_cost
    pre_total = post_total - price * qty   # fees NOT in cost basis
    return _PositionState(qty=pre_qty, avg_cost=pre_total / pre_qty)


def _unapply_sell(pos: _PositionState, qty: Decimal) -> _PositionState:
    """FIFO sell — un-applying restores qty; avg_cost is the post-sell
    aggregate. For the ticker-reopen case (pos.qty == 0), callers should
    use _unapply_sell_with_history instead so they can recover avg_cost
    from the FIFO lot_history side channel (spec § Failure modes)."""
    return _PositionState(qty=pos.qty + qty, avg_cost=pos.avg_cost)


def _unapply_sell_with_history(scope: AccountScope, ticker: str,
                               pos: _PositionState, qty: Decimal,
                               deal_id: str,
                               on_date: date) -> _PositionState:
    """C19: standard un-apply when pos.qty > 0; reopen recovery from
    _LOT_HISTORY_CACHE when pos.qty == 0. Falls back to writing an anomaly
    + qty/avg_cost=0 when lot_history is missing entirely."""
    if pos.qty > 0:
        return _unapply_sell(pos, qty)
    key = (scope.broker, scope.account_env, scope.broker_account, ticker)
    history = _LOT_HISTORY_CACHE.get(key, [])
    close_event = next((e for e in history
                        if e.get("kind") == "close"
                        and e.get("deal_id") == deal_id), None)
    if not close_event:
        write_recon_anomaly(scope, on_date,
                            notes={"anomaly_type": "missing_lot_history",
                                   "ticker": ticker, "deal_id": deal_id})
        return _PositionState(qty=qty, avg_cost=Decimal("0"))
    open_events = [e for e in history if e.get("kind") == "open"]
    pre_qty = Decimal("0")
    pre_cost_sum = Decimal("0")
    for ev in open_events:
        if ev["filled_at"] >= close_event["filled_at"]:
            break
        pre_qty += Decimal(str(ev["qty"]))
        pre_cost_sum += Decimal(str(ev["qty"])) * Decimal(str(ev["avg_cost"]))
    if pre_qty == 0:
        return _PositionState(qty=qty, avg_cost=Decimal("0"))
    return _PositionState(qty=pre_qty, avg_cost=pre_cost_sum / pre_qty)


def run_backward_walk(scope: AccountScope, anchor: AnchorState,
                      walk_to: date) -> int:
    """Walk back from anchor.today − 1 to walk_to (inclusive).

    C24: today's row is NOT written into nav_history here — the caller
    follows up with write_today_reported_nav, which writes the canonical
    today row as source='reported'. Instead, we drop the cost-basis anchor
    into the reconciliation log so the historical reconstruction can be
    audited against the live reported NAV.

    Historical rows are immutable: this function only INSERTs via the
    `is_today=False` guard inside write_nav_row.
    """
    # C24: write anchor as a recon-log row only, not as a nav_history row.
    nav_today_cost_basis = anchor.cost_basis_nav()
    threshold = max(Decimal("25"),
                    nav_today_cost_basis * Decimal("0.001"))
    write_recon_daily(scope, anchor.today,
                      reported=None,   # write_today_reported_nav fills this
                      reconstructed=nav_today_cost_basis,
                      threshold=threshold,
                      notes={"anchor_reason": "first_run_backfill"})
    rows_written = 0

    # Pre-fetch all events for the window and seed the lot-history cache
    # (C19) for every ticker whose FIFO might be needed during reopen
    # recovery.
    all_fills = list(fetch_fills(scope, walk_to,
                                 anchor.today + timedelta(days=1)))
    all_flows = list(fetch_cash_flows(scope, walk_to,
                                      anchor.today + timedelta(days=1)))
    tickers_in_history = {f["ticker"] for f in all_fills}
    for t in tickers_in_history:
        key = (scope.broker, scope.account_env, scope.broker_account, t)
        if key not in _LOT_HISTORY_CACHE:
            _rematch_ticker(scope, t)
    all_corp = list(fetch_corp_actions_for_tickers(list(tickers_in_history)))

    by_date_fills = _bucket_by_et_date(all_fills, "filled_at")
    by_date_flows = _bucket_by_et_date(all_flows, "flow_at")
    by_date_corp = _bucket_corp(all_corp)

    # C32: scope-wide deal-id set, not per-day. A T+1 settlement-date fee
    # whose deal_id ties to a fill on a different ET date would otherwise
    # be double-counted as a standalone cash flow.
    known_deal_ids = {f["futu_deal_id"] for f in all_fills}

    state_cash = anchor.cash
    state_pos = {t: _PositionState(p.qty, p.avg_cost)
                 for t, p in anchor.positions.items()}

    cursor = anchor.today
    while cursor > walk_to:
        next_day = cursor
        cursor = cursor - timedelta(days=1)

        # Un-apply fills from next_day.
        for fill in by_date_fills.get(next_day, []):
            pos = state_pos.get(
                fill["ticker"],
                _PositionState(qty=Decimal("0"), avg_cost=Decimal("0")))
            qty = Decimal(str(fill["quantity"]))
            price = Decimal(str(fill["price"]))
            fees = Decimal(str(fill["fees"]))
            if fill["action"] == "BUY":
                state_cash += price * qty + fees
                state_pos[fill["ticker"]] = _unapply_buy(pos, qty, price, fees)
            else:  # SELL
                state_cash -= price * qty - fees
                state_pos[fill["ticker"]] = _unapply_sell_with_history(
                    scope, fill["ticker"], pos, qty,
                    fill["futu_deal_id"], cursor)

        # Un-apply cash-flow events from next_day, skipping fee-on-deal
        # (scope-wide deal_id set per C32).
        for cf in by_date_flows.get(next_day, []):
            cf_deal = (cf["raw"] or {}).get("deal_id")
            if cf_deal and cf_deal in known_deal_ids:
                continue
            if cf["amount"] is None:
                continue  # missing-FX day — already anomaly-logged
            state_cash -= Decimal(str(cf["amount"]))

        # Un-apply corporate actions on next_day.
        for ca in by_date_corp.get(next_day, []):
            ratio = (Decimal(str(ca["ratio_numerator"])) /
                     Decimal(str(ca["ratio_denominator"])))
            tk = ca["ticker"]
            if tk in state_pos:
                state_pos[tk] = _PositionState(
                    qty=state_pos[tk].qty / ratio,
                    avg_cost=state_pos[tk].avg_cost * ratio)
            if ca["action_type"] == "symbol_change" and ca.get("new_ticker"):
                state_pos[ca["new_ticker"]] = state_pos.pop(tk, _PositionState(
                    qty=Decimal("0"), avg_cost=Decimal("0")))

        stock = sum((p.qty * p.avg_cost for p in state_pos.values()),
                    Decimal("0"))
        nav_D = state_cash + stock
        write_nav_row(scope, cursor, nav_D, source="reconstructed",
                      cash=state_cash, stock_value=stock)
        rows_written += 1

    return rows_written


def _bucket_by_et_date(rows: list[dict], ts_field: str) -> dict[date, list[dict]]:
    from zoneinfo import ZoneInfo
    bucket: dict[date, list[dict]] = {}
    for r in rows:
        ts = r[ts_field]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        et_date = ts.astimezone(ZoneInfo("America/New_York")).date()
        bucket.setdefault(et_date, []).append(r)
    return bucket


def _bucket_corp(rows: list[dict]) -> dict[date, list[dict]]:
    bucket: dict[date, list[dict]] = {}
    for r in rows:
        bucket.setdefault(r["effective_date"], []).append(r)
    return bucket


def write_today_reported_nav(scope: AccountScope, trade_ctx,
                             today: date) -> Decimal:
    """Daily forward: write today's reported NAV from accinfo + UPSERT recon
    row.

    C26: Futu emits `total_assets`, not `net_liquidation`. The xenon
    nav_history column is still called `nav`; we use `total_assets` as the
    canonical reported NAV.

    C24: write_recon_daily UPSERTs (not insert-only) so the anchor row
    written by run_backward_walk (which has reported=NULL, reconstructed=
    cost_basis_NAV) merges with this row's reported_nav. End result: one
    daily recon row whose `reconstructed_nav` is the historical cost-basis
    anchor and whose `reported_nav` is today's broker-reported NAV.
    """
    code, accinfo_df = trade_ctx.accinfo_query(
        acc_id=scope.broker_account, trd_env="REAL")
    summary = accinfo_df.to_dict(orient="records")[0]
    raw_total = summary.get("total_assets")
    if raw_total is None:
        raise RuntimeError(
            "Futu accinfo_query returned no `total_assets` column — "
            "verify SDK version against C26 (run Task 5 Step 0 probe)")
    reported = Decimal(str(raw_total))
    cash = Decimal(str(summary.get("cash", 0)))
    unrealized = Decimal(str(summary.get("unrealized_pl", 0)))
    cost_basis = reported - unrealized
    write_nav_row(scope, today, reported, source="reported",
                  cash=cash, stock_value=reported - cash, is_today=True)
    threshold = max(Decimal("25"), reported * Decimal("0.001"))
    write_recon_daily(scope, today, reported=reported,
                      reconstructed=cost_basis, threshold=threshold,
                      notes={"unrealized_pl_today": str(unrealized)})
    return reported
```

Update `_run_sync_under_lock` to add step 7 at the tail. C25: `--rematch`
is the only operator-explicit lever that recomputes historical NAV; it
must (a) re-enter the walk branch even when backfill_done_at is set, and
(b) DELETE existing reconstructed nav_history rows + clear
\_LOT_HISTORY_CACHE for this scope before the walk so the rewrite is fresh.

```python
        # Step 7: NAV — backward walk if first run OR --rematch; today
        # always.
        backfill_done_at = list(fetch_nav_history(scope,
                                                  MIN_HISTORICAL_DATE,
                                                  MIN_HISTORICAL_DATE))
        nav_rows_added = 0
        if rematch or not backfill_done_at:
            if rematch:
                # C25: wipe historical reconstructed rows so the walk can
                # rewrite them with corrected values.
                with get_sync_engine().begin() as conn:
                    conn.execute(text(f"""
                        DELETE FROM xenon.nav_history
                         WHERE {_scope_filter()}
                           AND source = 'reconstructed'
                           AND date < :today
                    """), {**_scope_params(scope), "today": today})
                # And drop this scope's lot-history cache entries.
                keys_to_drop = [k for k in _LOT_HISTORY_CACHE
                                if k[:3] == (scope.broker,
                                             scope.account_env,
                                             scope.broker_account)]
                for k in keys_to_drop:
                    del _LOT_HISTORY_CACHE[k]
            anchor = _build_anchor_from_live_account(scope)
            anchor = AnchorState(today=today, cash=anchor.cash,
                                 positions=anchor.positions)
            nav_rows_added = run_backward_walk(scope, anchor,
                                               walk_to=MIN_HISTORICAL_DATE)
        write_today_reported_nav(scope, trade_ctx, today)

        result = {
            "status": "ok", "fills_added": fills_added,
            "cash_flows_added": cash_flows_added,
            "nav_rows_added": nav_rows_added, "warnings": warnings,
        }
        return result
```

- [ ] **Step 11: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_nav_backwalk.py \
              scripts/tests/test_futu_backwalk_corp_action_reversal.py \
              scripts/tests/test_futu_backwalk_ticker_reopen_missing_lot_history.py \
              scripts/tests/test_futu_nav_historical_immutable.py \
              scripts/tests/test_futu_nav_today_upsert.py \
              scripts/tests/test_futu_nav_continue_advances_state.py \
              scripts/tests/test_futu_nav_past_missing_is_reconstructed.py \
              scripts/tests/test_futu_reconciliation_log.py -xvs
```

- [ ] **Step 12: Stage**

```bash
git add src/xenon/api/services/futu_history.py scripts/tests/test_futu_nav_*.py \
        scripts/tests/test_futu_backwalk_*.py \
        scripts/tests/test_unapply_directions.py \
        scripts/tests/test_futu_rematch_recomputes_historical.py \
        scripts/tests/test_futu_reconciliation_log.py
```

Stop.

---

## Milestone 6 — CLI + Schedule (Tasks 15-16)

### Task 15: `xenon-futu-history-sync` CLI entry point

**Files:**

- Create: `scripts/futu_history_sync.py`
- Modify: `pyproject.toml` (add `[project.scripts]` entry)
- Test: `scripts/tests/test_futu_history_sync_cli.py`

- [ ] **Step 1: Write the CLI test**

```python
# scripts/tests/test_futu_history_sync_cli.py
"""CLI smoke: --help prints usage; --account + --since are forwarded."""
import subprocess
import sys


def test_help_exits_zero():
    r = subprocess.run(["uv", "run", "xenon-futu-history-sync", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "--since" in r.stdout
    assert "--account" in r.stdout


def test_invalid_since_returns_nonzero():
    r = subprocess.run(
        ["uv", "run", "xenon-futu-history-sync",
         "--account", "test", "--since", "yesterday"],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "since" in r.stderr.lower()
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_history_sync_cli.py -xvs
```

- [ ] **Step 3: Implement**

```python
# scripts/futu_history_sync.py
"""CLI entry point — invoked by launchd at 16:30 ET daily, and by the operator
on-demand for catch-up syncs.

Usage:
    uv run xenon-futu-history-sync --account ACC [--since auto|YYYY-MM-DD]
                                                 [--rematch]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from xenon.api.services.futu_history import (
    futu_history_sync, parse_since, SinceUnparseable, SinceOutOfRange,
)
from xenon.execution.account_scope import AccountScope

log = logging.getLogger("xenon.futu_history_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xenon-futu-history-sync")
    parser.add_argument("--account", required=True,
                        help="Futu broker_account (scope identifier)")
    parser.add_argument("--env", default="live",
                        choices=["paper", "live", "sim", "legacy_unknown"])
    parser.add_argument("--since", default="auto",
                        help="auto|YYYY-MM-DD (floor 2024-05-01)")
    parser.add_argument("--rematch", action="store_true",
                        help="Force full FIFO rematch + walk-back recompute")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        since = parse_since(args.since)
    except SinceUnparseable as exc:
        print(f"error: --since unparseable: {exc.bad_value!r}",
              file=sys.stderr)
        return 2
    except SinceOutOfRange as exc:
        print(f"error: --since out of range: {exc.bad_value!r}",
              file=sys.stderr)
        return 2

    scope = AccountScope(broker="FUTU", account_env=args.env,
                  broker_account=args.account)
    result = futu_history_sync(scope=scope, since=since,
                               rematch=args.rematch)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") in {"ok", "in_progress"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `pyproject.toml` under `[project.scripts]`:

```toml
xenon-futu-history-sync = "scripts.futu_history_sync:main"
```

Then sync:

```bash
uv sync
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_history_sync_cli.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add scripts/futu_history_sync.py pyproject.toml uv.lock \
        scripts/tests/test_futu_history_sync_cli.py
```

Stop.

---

### Task 16: launchd plist + dev install helper

**Files:**

- Create: `scripts/launchd/com.xenon.futu-history-sync.plist`
- Create: `scripts/launchd/install_futu_history_sync.sh`

This task is install scaffolding only — no automated test. After Task 22 (e2e), the operator runs the install script manually on the dev Mac (Phase 1) and later on the Mac mini (Phase 2).

- [ ] **Step 1: Write the plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xenon.futu-history-sync</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-c</string>
        <string>cd /Users/chenxi/projects/xenon &amp;&amp; TZ=America/New_York /opt/homebrew/bin/uv run xenon-futu-history-sync --account __FUTU_ACCOUNT__ --env live --since auto &gt;&gt; /tmp/xenon-futu-history-sync.log 2&gt;&amp;1</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>/tmp/xenon-futu-history-sync.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/xenon-futu-history-sync.stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Write the install helper**

```bash
#!/usr/bin/env bash
# scripts/launchd/install_futu_history_sync.sh
#
# Installs the launchd job that fires `xenon-futu-history-sync` at 16:30 ET daily.
# Idempotent: re-running unloads and re-loads with the latest plist body.
#
# Usage:
#   FUTU_ACCOUNT=12345 ./scripts/launchd/install_futu_history_sync.sh

set -euo pipefail

: "${FUTU_ACCOUNT:?FUTU_ACCOUNT env var required (Futu broker account id)}"

SRC=$(dirname "$0")/com.xenon.futu-history-sync.plist
DEST="$HOME/Library/LaunchAgents/com.xenon.futu-history-sync.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s/__FUTU_ACCOUNT__/${FUTU_ACCOUNT}/" "$SRC" > "$DEST"

if launchctl list | grep -q com.xenon.futu-history-sync; then
    launchctl unload "$DEST"
fi
launchctl load "$DEST"
echo "Loaded $DEST"
launchctl list | grep com.xenon.futu-history-sync
```

```bash
chmod +x scripts/launchd/install_futu_history_sync.sh
```

- [ ] **Step 3: Stage (no test; operator-installed)**

```bash
git add scripts/launchd/com.xenon.futu-history-sync.plist \
        scripts/launchd/install_futu_history_sync.sh
```

Stop.

---

## Milestone 7 — API surface (Tasks 17-20)

### Task 17: Routes scaffold + `POST /futu/history/sync` + `GET /futu-stats`

**Files:**

- Create: `src/xenon/api/routes/futu_history.py`
- Modify: `src/xenon/api/guards.py` (add `get_futu_account_scope` — C29)
- Modify: `src/xenon/api/server.py` (register router; mirror existing `uw_analyze` registration)
- Test: `scripts/tests/test_futu_route_sync.py`
- Test: `scripts/tests/test_futu_stats_route.py` (C11)

- [ ] **Step 0: Add the FUTU-specific scope dependency (C29)**

`get_account_scope` reads `app.state.broker` which defaults to `IB`. In
production, Next.js callers don't set `XENON_BROKER=FUTU`, so `/futu/*`
resolves to the IB scope and returns empty data. Add a Futu-specific
dependency that always returns an `AccountScope(broker="FUTU", ...)`.

```python
# Append to src/xenon/api/guards.py

def get_futu_account_scope(request: Request) -> AccountScope:
    """Resolve the FUTU account scope. Reads `XENON_FUTU_ACCOUNT` from env
    (configured in `.env`), with `account_env` mirroring the current
    trading mode. Falls back to `app.state.futu_account` (dev/test path)
    when the env var is unset."""
    import os
    account = os.environ.get("XENON_FUTU_ACCOUNT")
    if not account:
        ib_scope = resolve_from_app_state(request.app.state)
        account = getattr(request.app.state, "futu_account",
                          ib_scope.broker_account)
    return AccountScope(broker="FUTU",
                        account_env=request.app.state.trading_mode,
                        broker_account=account)
```

Document `XENON_FUTU_ACCOUNT` in the root `.env` (xenon CLAUDE.md
credentials table). Every `/futu/*` route below uses `get_futu_account_scope`;
the cross-broker route (Task 20) calls BOTH `get_account_scope` (IB side)
and `get_futu_account_scope` (FUTU side).

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_futu_route_sync.py
"""POST /futu/history/sync — scope-gated, validates `since`, returns
sync result envelope."""
from datetime import date
from unittest.mock import patch
from fastapi.testclient import TestClient

from xenon.api.server import app


def test_invalid_since_returns_422(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.post("/futu/history/sync", json={"since": "yesterday"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "since_unparseable"


def test_out_of_range_since_returns_422(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.post("/futu/history/sync", json={"since": "2020-01-01"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "since_out_of_range"


def test_auto_returns_envelope(futu_pytest_app_scope):
    with patch("xenon.api.routes.futu_history.futu_history_sync") as mock_run:
        mock_run.return_value = {
            "status": "ok", "fills_added": 0, "cash_flows_added": 0,
            "nav_rows_added": 0, "warnings": [],
        }
        with TestClient(app) as client:
            r = client.post("/futu/history/sync", json={"since": "auto"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "fills_added" in body
```

Also write the `/futu-stats` test (C11):

```python
# scripts/tests/test_futu_stats_route.py
"""GET /futu-stats — exposes FutuApiStats.snapshot for the sidebar."""
from fastapi.testclient import TestClient
from xenon.api.server import app


def test_futu_stats_returns_snapshot_envelope(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.get("/futu-stats")
    assert r.status_code == 200
    body = r.json()
    # Snapshot envelope shape from FutuApiStats.snapshot().
    assert "calls" in body or "totals" in body  # both names accepted
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_route_sync.py -xvs
```

- [ ] **Step 3: Implement the router**

```python
# src/xenon/api/routes/futu_history.py
"""FastAPI router for Futu history surfaces.

Per src/xenon/api/CLAUDE.md: routers stay thin, services own the logic.
Every route is scope-gated via xenon.api.guards.get_futu_account_scope
(C29) — get_account_scope reads the IB scope by default, so /futu/*
routes would return empty data in production without this override.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from xenon.api.guards import get_futu_account_scope
from xenon.api.services.futu_history import (
    futu_history_sync, parse_since,
    SinceUnparseable, SinceOutOfRange, MIN_HISTORICAL_DATE,
)
from xenon.execution.account_scope import AccountScope
from xenon.utils.futu_api_stats import FutuApiStats

router = APIRouter(tags=["futu_history"])


class SyncBody(BaseModel):
    since: str = "auto"
    rematch: bool = False


@router.post("/futu/history/sync")
def post_sync(body: SyncBody,
              scope: AccountScope = Depends(get_futu_account_scope)) -> dict[str, Any]:
    try:
        since = parse_since(body.since)
    except SinceUnparseable as exc:
        raise HTTPException(status_code=422, detail={
            "reason_code": exc.reason_code, "bad_value": exc.bad_value})
    except SinceOutOfRange as exc:
        raise HTTPException(status_code=422, detail={
            "reason_code": exc.reason_code, "bad_value": exc.bad_value,
            "min_date": MIN_HISTORICAL_DATE.isoformat()})
    return futu_history_sync(scope=scope, since=since, rematch=body.rematch)


# C11: observability sidebar route. Mirrors /uw-stats — no auth dep.
@router.get("/futu-stats")
def get_futu_stats() -> dict:
    return FutuApiStats.instance().snapshot()
```

In `src/xenon/api/server.py`, find the spot where other routers are registered (search for `app.include_router(uw_analyze`) and add:

```python
from xenon.api.routes import futu_history as futu_history_routes
app.include_router(futu_history_routes.router)
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_route_sync.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/api/routes/futu_history.py \
        src/xenon/api/guards.py \
        src/xenon/api/server.py \
        scripts/tests/test_futu_route_sync.py \
        scripts/tests/test_futu_stats_route.py
```

Stop.

---

### Task 18: `GET /futu/equity-curve` + `GET /futu/reconciliation`

**Files:**

- Modify: `src/xenon/api/routes/futu_history.py`
- Modify: `src/xenon/api/services/futu_history.py` (add query helpers)
- Test: `scripts/tests/test_futu_route_equity_curve.py`
- Test: `scripts/tests/test_futu_route_reconciliation.py`

- [ ] **Step 1: Write the tests**

```python
# scripts/tests/test_futu_route_equity_curve.py
from datetime import date
from decimal import Decimal
from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    write_nav_row,
    write_recon_daily,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_equity_curve_returns_rows(clean_scope, futu_pytest_app_scope):
    write_nav_row(SCOPE, date(2024, 5, 1), Decimal("10000"),
                  "reconstructed", Decimal("10000"), Decimal("0"))
    write_nav_row(SCOPE, date(2024, 5, 2), Decimal("10100"),
                  "reported", Decimal("10000"), Decimal("100"),
                  is_today=True)
    write_recon_daily(SCOPE, date(2024, 5, 2), Decimal("10100"),
                      Decimal("10000"), Decimal("25"))
    with TestClient(app) as client:
        r = client.get("/futu/equity-curve",
                       params={"from": "2024-05-01", "to": "2024-05-02"})
    assert r.status_code == 200
    rows = r.json()
    by_d = {row["date"]: row for row in rows}
    assert by_d["2024-05-01"]["source"] == "reconstructed"
    assert by_d["2024-05-02"]["recon"]["delta"] == 100.0


def test_equity_curve_validates_range(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.get("/futu/equity-curve",
                       params={"from": "2020-01-01", "to": "2024-05-01"})
    assert r.status_code == 422


def test_equity_curve_rejects_future_date(futu_pytest_app_scope):
    # C33: upper bound.
    with TestClient(app) as client:
        r = client.get("/futu/equity-curve",
                       params={"from": "2024-05-01", "to": "2050-01-01"})
    assert r.status_code == 422


def test_equity_curve_rejects_inverted_range(futu_pytest_app_scope):
    # C33: from > to.
    with TestClient(app) as client:
        r = client.get("/futu/equity-curve",
                       params={"from": "2024-12-01", "to": "2024-06-01"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "date_range_inverted"
```

```python
# scripts/tests/test_futu_route_reconciliation.py
from datetime import date
from decimal import Decimal
from fastapi.testclient import TestClient
from xenon.api.server import app
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    write_recon_daily,
    write_recon_anomaly,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_reconciliation_returns_latest_30(clean_scope, futu_pytest_app_scope):
    write_recon_daily(SCOPE, date(2024, 5, 1), Decimal("100"),
                      Decimal("100"), Decimal("25"))
    write_recon_anomaly(SCOPE, date(2024, 5, 1),
                        notes={"anomaly_type": "missing_fx",
                               "currency": "HKD"})
    with TestClient(app) as client:
        r = client.get("/futu/reconciliation")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "current_delta" in body
    assert any(row["kind"] == "anomaly" for row in body["rows"])
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_route_equity_curve.py \
              scripts/tests/test_futu_route_reconciliation.py -xvs
```

- [ ] **Step 3: Add query helpers to the service**

Append to `src/xenon/api/services/futu_history.py`:

```python
def get_equity_curve(scope: AccountScope, start: date,
                     end: date) -> list[dict]:
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries.futu_history import fetch_nav_history
    # Pull NAV + reconciliation rows in two queries; merge in Python.
    nav_rows = list(fetch_nav_history(scope, start, end))
    with get_sync_engine().connect() as conn:
        recon_rows = conn.execute(text(f"""
            SELECT recon_date, delta, exceeded, notes
              FROM xenon.futu_reconciliation_log
             WHERE {_scope_filter()} AND kind='daily'
               AND recon_date BETWEEN :start AND :end
        """), {**_scope_params(scope),
               "start": start, "end": end}).fetchall()
    recon_by_date = {r.recon_date: r for r in recon_rows}
    out: list[dict] = []
    prev_nav: Decimal | None = None
    for row in nav_rows:
        recon = recon_by_date.get(row["date"])
        daily_pnl = (Decimal(str(row["nav"])) - prev_nav) \
                    if prev_nav is not None else None
        prev_nav = Decimal(str(row["nav"]))
        out.append({
            "date": row["date"].isoformat(),
            "nav": float(row["nav"]),
            "source": row["source"],
            "daily_pnl": float(daily_pnl) if daily_pnl is not None else None,
            "recon": ({"delta": float(recon.delta) if recon.delta else 0.0,
                       "exceeded": bool(recon.exceeded),
                       "notes": recon.notes} if recon else None),
        })
    return out


def get_reconciliation_summary(scope: AccountScope, limit: int = 30) -> dict:
    from xenon.db.engine import get_sync_engine
    with get_sync_engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT kind, recon_date, reported_nav, reconstructed_nav,
                   delta, threshold, exceeded, notes, computed_at
              FROM xenon.futu_reconciliation_log
             WHERE {_scope_filter()}
             ORDER BY computed_at DESC
             LIMIT :lim
        """), {**_scope_params(scope), "lim": limit}).fetchall()
        current = conn.execute(text(f"""
            SELECT delta FROM xenon.futu_reconciliation_log
             WHERE {_scope_filter()} AND kind='daily'
             ORDER BY recon_date DESC LIMIT 1
        """), _scope_params(scope)).scalar()
    return {
        "current_delta": float(current) if current is not None else None,
        "rows": [{
            "kind": r.kind, "recon_date": r.recon_date.isoformat(),
            "reported_nav": float(r.reported_nav)
                            if r.reported_nav is not None else None,
            "reconstructed_nav": float(r.reconstructed_nav)
                                 if r.reconstructed_nav is not None else None,
            "delta": float(r.delta) if r.delta is not None else None,
            "threshold": float(r.threshold) if r.threshold is not None else None,
            "exceeded": bool(r.exceeded) if r.exceeded is not None else None,
            "notes": r.notes,
            "computed_at": r.computed_at.isoformat(),
        } for r in rows],
    }
```

- [ ] **Step 4: Add the routes**

Append to `src/xenon/api/routes/futu_history.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from xenon.api.services.futu_history import (
    get_equity_curve, get_reconciliation_summary,
)


def _validate_date(s: str, name: str) -> date:
    """C33: include the upper bound and emit a structured error envelope."""
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=422, detail={
            "reason_code": "date_unparseable", "param": name, "value": s})
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if d < MIN_HISTORICAL_DATE or d > today_et:
        raise HTTPException(status_code=422, detail={
            "reason_code": "date_out_of_range", "param": name, "value": s,
            "min_date": MIN_HISTORICAL_DATE.isoformat(),
            "max_date": today_et.isoformat()})
    return d


def _validate_range(start: date, end: date) -> None:
    """C33: reject from > to (was silently empty before)."""
    if end < start:
        raise HTTPException(status_code=422, detail={
            "reason_code": "date_range_inverted",
            "from": start.isoformat(), "to": end.isoformat()})


@router.get("/futu/equity-curve")
def get_equity_curve_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    scope: AccountScope = Depends(get_futu_account_scope),
) -> list[dict]:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    return get_equity_curve(scope, start, end)


@router.get("/futu/reconciliation")
def get_reconciliation_route(
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    return get_reconciliation_summary(scope)
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_route_equity_curve.py \
              scripts/tests/test_futu_route_reconciliation.py -xvs
```

- [ ] **Step 6: Stage**

```bash
git add src/xenon/api/routes/futu_history.py \
        src/xenon/api/services/futu_history.py \
        scripts/tests/test_futu_route_equity_curve.py \
        scripts/tests/test_futu_route_reconciliation.py
```

Stop.

---

### Task 19: `GET /futu/trades` + `GET /futu/performance`

**Files:**

- Modify: `src/xenon/api/routes/futu_history.py`
- Modify: `src/xenon/api/services/futu_history.py`
- Test: `scripts/tests/test_futu_route_trades.py`
- Test: `scripts/tests/test_futu_route_performance.py`
- Test: `scripts/tests/test_futu_twr_excludes_deposits.py`

- [ ] **Step 1: Write the tests**

```python
# scripts/tests/test_futu_route_trades.py
from datetime import date, datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient
from xenon.api.server import app
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import insert_fills

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _f(deal_id, t):
    return dict(
        futu_deal_id=deal_id, futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action="BUY", quantity=1,
        price=Decimal("100"), fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t), raw={},
    )


def test_pagination_with_cursor(clean_scope, futu_pytest_app_scope):
    insert_fills(SCOPE, [
        _f("d1", "2024-05-01T10:00:00+00:00"),
        _f("d2", "2024-05-02T10:00:00+00:00"),
        _f("d3", "2024-05-03T10:00:00+00:00"),
    ])
    with TestClient(app) as client:
        r1 = client.get("/futu/trades", params={
            "from": "2024-05-01", "to": "2024-05-03",
            "limit": 2, "sort": "filled_at", "direction": "desc"})
        body1 = r1.json()
        assert len(body1["rows"]) == 2
        assert body1["next_cursor"] is not None
        r2 = client.get("/futu/trades", params={
            "from": "2024-05-01", "to": "2024-05-03",
            "limit": 2, "sort": "filled_at", "direction": "desc",
            "cursor": body1["next_cursor"]})
        body2 = r2.json()
        assert len(body2["rows"]) == 1
        assert body2["next_cursor"] is None


def test_cursor_requires_filled_at_sort(clean_scope, futu_pytest_app_scope):
    """C34: cursor + sort=realized_pnl returns 422 because the cursor
    predicate is keyed on filled_at."""
    with TestClient(app) as client:
        r = client.get("/futu/trades", params={
            "from": "2024-05-01", "to": "2024-05-03",
            "sort": "realized_pnl", "cursor": "anything"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "cursor_requires_filled_at_sort"
```

```python
# scripts/tests/test_futu_twr_excludes_deposits.py
from datetime import date, datetime, timezone
from decimal import Decimal
from xenon.api.services.futu_history import compute_performance_stats
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import (
    write_nav_row,
    insert_cash_flows,
)

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_deposit_with_zero_market_move_yields_zero_twr(clean_scope):
    write_nav_row(SCOPE, date(2024, 5, 1), Decimal("0"),
                  "reconstructed", Decimal("0"), Decimal("0"))
    write_nav_row(SCOPE, date(2024, 5, 2), Decimal("1000"),
                  "reported", Decimal("1000"), Decimal("0"),
                  is_today=True)
    insert_cash_flows(SCOPE, [dict(
        futu_flow_id="f1", flow_type="deposit", futu_type_raw="FUND_IN",
        amount=Decimal("1000"), amount_original=Decimal("1000"),
        original_currency="USD", fx_rate_used=None, ticker=None,
        description="wire", flow_at=datetime(
            2024, 5, 2, 9, tzinfo=timezone.utc), raw={})])
    stats = compute_performance_stats(SCOPE, date(2024, 5, 1),
                                      date(2024, 5, 2))
    assert abs(stats["total_return_twr"]) < 0.001
```

```python
# scripts/tests/test_max_drawdown_uses_running_peak.py
"""C35: max drawdown must track the running peak so a V-shape correctly
shows a -50% drawdown."""
from datetime import date
from decimal import Decimal
from xenon.api.services.futu_history import compute_performance_stats
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import write_nav_row

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_v_shape_drawdown(clean_scope):
    # 100 -> 50 -> 200. Running-peak DD on the 50 point = (50-100)/100 = -50%.
    # Global-peak DD would be 0% (peak=200, no post-peak trough).
    write_nav_row(SCOPE, date(2024, 5, 1), Decimal("100"),
                  "reconstructed", Decimal("100"), Decimal("0"))
    write_nav_row(SCOPE, date(2024, 5, 2), Decimal("50"),
                  "reconstructed", Decimal("50"), Decimal("0"))
    write_nav_row(SCOPE, date(2024, 5, 3), Decimal("200"),
                  "reported", Decimal("200"), Decimal("0"), is_today=True)
    stats = compute_performance_stats(SCOPE, date(2024, 5, 1),
                                      date(2024, 5, 3))
    assert abs(stats["max_drawdown"] - (-0.5)) < 0.001
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest scripts/tests/test_futu_route_trades.py \
              scripts/tests/test_futu_twr_excludes_deposits.py -xvs
```

- [ ] **Step 3: Implement trades + performance helpers**

Append to `src/xenon/api/services/futu_history.py`:

```python
import base64
import json as _json


def encode_cursor(filled_at, deal_id: str) -> str:
    payload = {"t": filled_at.isoformat(), "id": deal_id}
    return base64.urlsafe_b64encode(
        _json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(c: str) -> dict | None:
    if not c:
        return None
    pad = "=" * (-len(c) % 4)
    return _json.loads(base64.urlsafe_b64decode(c + pad).decode())


def list_trades(scope: AccountScope, start: date, end: date, *,
                ticker: str | None = None, action: str | None = None,
                sort: str = "filled_at", direction: str = "desc",
                limit: int = 50, cursor: str | None = None) -> dict:
    # C34: the cursor predicate compares `(filled_at, futu_deal_id)`, so it
    # only makes sense alongside `sort=filled_at`. Pairing it with another
    # sort produces meaningless skip/repeat behavior — reject loudly.
    from fastapi import HTTPException
    if cursor and sort != "filled_at":
        raise HTTPException(status_code=422, detail={
            "reason_code": "cursor_requires_filled_at_sort"})
    cur = decode_cursor(cursor) if cursor else None
    order = "DESC" if direction.lower() == "desc" else "ASC"
    sort_col = {"filled_at": "filled_at",
                "realized_pnl": "realized_pnl",
                "ticker": "ticker",
                "fees": "fees"}.get(sort, "filled_at")
    where_parts = [
        _scope_filter(),
        f"(filled_at AT TIME ZONE 'America/New_York')::date BETWEEN :start AND :end",
    ]
    params: dict = {**_scope_params(scope), "start": start, "end": end,
                    "lim": limit + 1}
    if ticker:
        where_parts.append("ticker = :ticker")
        params["ticker"] = ticker.upper()
    if action:
        where_parts.append("action = :action")
        params["action"] = action.upper()
    if cur:
        comparator = "<" if order == "DESC" else ">"
        where_parts.append(
            f"(filled_at, futu_deal_id) {comparator} (:cur_t, :cur_id)")
        params["cur_t"] = cur["t"]
        params["cur_id"] = cur["id"]
    sql = text(f"""
        SELECT futu_deal_id, ticker, action, quantity, price, fees,
               realized_pnl, filled_at, futu_order_id
          FROM xenon.futu_trades
         WHERE {' AND '.join(where_parts)}
         ORDER BY {sort_col} {order}, futu_deal_id {order}
         LIMIT :lim
    """)
    with get_sync_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(sql, params)]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last["filled_at"], last["futu_deal_id"])
        rows = rows[:limit]
    return {
        "rows": [{
            "futu_deal_id": r["futu_deal_id"], "ticker": r["ticker"],
            "action": r["action"], "quantity": int(r["quantity"]),
            "price": float(r["price"]), "fees": float(r["fees"]),
            "realized_pnl": (float(r["realized_pnl"])
                             if r["realized_pnl"] is not None else None),
            "filled_at": r["filled_at"].isoformat(),
        } for r in rows],
        "next_cursor": next_cursor,
    }


def compute_performance_stats(scope: AccountScope, start: date, end: date) -> dict:
    """TWR by sub-periods bracketed by external cash flows + closed-trade
    aggregations.

    C35: max_drawdown uses the running peak walking left-to-right (not the
    global max). The V-shape series 100 → 50 → 200 has global peak 200,
    trough-after-peak 200 → 0% drawdown. The actual max drawdown on that
    series is (50 − 100) / 100 = −50%; a running peak captures that.

    C8: spec promises win_rate, avg_holding_days, best/worst trade,
    total_commissions, monthly_pnl. They're populated here from the closed
    SELL rows + their matching FIFO BUY events on the same ticker.
    """
    from xenon.db.queries.futu_history import (
        fetch_nav_history,
        fetch_cash_flows,
    )
    nav_series = list(fetch_nav_history(scope, start, end))
    flows = [cf for cf in fetch_cash_flows(scope, start, end)
             if cf["flow_type"] in ("deposit", "withdraw")
             and cf["amount"] is not None]
    if len(nav_series) < 2:
        return _empty_stats()

    nav_by_date = {r["date"]: Decimal(str(r["nav"])) for r in nav_series}
    flow_by_date: dict[date, Decimal] = {}
    for cf in flows:
        d = cf["flow_at"].date()
        flow_by_date[d] = flow_by_date.get(d, Decimal("0")) + Decimal(
            str(cf["amount"]))

    sorted_dates = sorted(nav_by_date.keys())
    sub_returns: list[Decimal] = []
    prev_d = sorted_dates[0]
    for d in sorted_dates[1:]:
        nav_start = nav_by_date[prev_d]
        nav_end = nav_by_date[d]
        cf = flow_by_date.get(d, Decimal("0"))
        if nav_start == 0:
            sub_returns.append(Decimal("0"))
        else:
            sub_returns.append((nav_end - nav_start - cf) / nav_start)
        prev_d = d

    twr = Decimal("1")
    for r in sub_returns:
        twr *= (Decimal("1") + r)
    twr -= Decimal("1")

    days = (sorted_dates[-1] - sorted_dates[0]).days or 1
    cagr = ((Decimal("1") + twr) ** (Decimal("365") / Decimal(days))
            - Decimal("1"))

    # C35: running peak (not global) so a V-shape sees the trough as a
    # drawdown.
    sorted_navs = [nav_by_date[d] for d in sorted_dates]
    running_peak = sorted_navs[0]
    max_dd = Decimal("0")
    for nav in sorted_navs:
        if nav > running_peak:
            running_peak = nav
        dd = ((nav - running_peak) / running_peak
              if running_peak > 0 else Decimal("0"))
        if dd < max_dd:
            max_dd = dd

    # C8: closed-trade aggregations from xenon.futu_trades.
    closed_sql = text(f"""
        SELECT ticker, action, quantity, price, fees, realized_pnl, filled_at
          FROM xenon.futu_trades
         WHERE {_scope_filter()}
           AND (filled_at AT TIME ZONE 'America/New_York')::date
               BETWEEN :start AND :end
         ORDER BY ticker, filled_at
    """)
    with get_sync_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            closed_sql, {**_scope_params(scope), "start": start, "end": end})]

    closed_sells = [r for r in rows
                    if r["action"] == "SELL"
                    and r["realized_pnl"] is not None]
    wins = [r for r in closed_sells if r["realized_pnl"] > 0]
    win_rate = (len(wins) / len(closed_sells)) if closed_sells else None
    best = max(closed_sells, key=lambda r: r["realized_pnl"], default=None)
    worst = min(closed_sells, key=lambda r: r["realized_pnl"], default=None)
    total_commissions = sum((Decimal(str(r["fees"])) for r in rows),
                            Decimal("0"))

    # Holding days: per SELL, find first prior BUY on same ticker (FIFO).
    holding_days: list[int] = []
    open_buys_by_ticker: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda x: x["filled_at"]):
        if r["action"] == "BUY":
            open_buys_by_ticker.setdefault(r["ticker"], []).append(r)
        elif r["action"] == "SELL" and open_buys_by_ticker.get(r["ticker"]):
            opening = open_buys_by_ticker[r["ticker"]].pop(0)
            holding_days.append((r["filled_at"].date()
                                 - opening["filled_at"].date()).days)
    avg_hd = (sum(holding_days) / len(holding_days)) if holding_days else None

    monthly: dict[str, float] = {}
    for r in closed_sells:
        key = r["filled_at"].strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + float(r["realized_pnl"])

    return {
        "total_return_twr": float(twr),
        "cagr": float(cagr),
        "max_drawdown": float(max_dd),
        "win_rate": win_rate,
        "avg_holding_days": avg_hd,
        "best_trade": ({"ticker": best["ticker"],
                        "realized_pnl": float(best["realized_pnl"]),
                        "filled_at": best["filled_at"].isoformat()}
                       if best else None),
        "worst_trade": ({"ticker": worst["ticker"],
                         "realized_pnl": float(worst["realized_pnl"]),
                         "filled_at": worst["filled_at"].isoformat()}
                        if worst else None),
        "total_commissions": float(total_commissions),
        "monthly_pnl": monthly,
        "external_cash_flows": {
            "deposits": float(sum((cf["amount"] for cf in flows
                                   if cf["flow_type"] == "deposit"
                                   and cf["amount"] is not None),
                                  Decimal("0"))),
            "withdrawals": float(sum((cf["amount"] for cf in flows
                                      if cf["flow_type"] == "withdraw"
                                      and cf["amount"] is not None),
                                     Decimal("0"))),
        },
    }


def _empty_stats() -> dict:
    return {"total_return_twr": 0.0, "cagr": 0.0, "max_drawdown": 0.0,
            "win_rate": None, "avg_holding_days": None,
            "best_trade": None, "worst_trade": None,
            "total_commissions": None, "monthly_pnl": {},
            "external_cash_flows": {"deposits": 0.0, "withdrawals": 0.0}}
```

Append routes to `src/xenon/api/routes/futu_history.py`:

```python
from xenon.api.services.futu_history import (
    list_trades, compute_performance_stats,
)


@router.get("/futu/trades")
def get_trades_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    ticker: str | None = None,
    action: str | None = None,
    sort: str = "filled_at",
    direction: str = "desc",
    limit: int = 50,
    cursor: str | None = None,
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    if sort not in {"filled_at", "realized_pnl", "ticker", "fees"}:
        raise HTTPException(status_code=422, detail={
            "reason_code": "sort_invalid", "value": sort})
    return list_trades(scope, start, end, ticker=ticker, action=action,
                       sort=sort, direction=direction, limit=limit,
                       cursor=cursor)


@router.get("/futu/performance")
def get_performance_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    return compute_performance_stats(scope, start, end)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest scripts/tests/test_futu_route_trades.py \
              scripts/tests/test_futu_twr_excludes_deposits.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/api/routes/futu_history.py \
        src/xenon/api/services/futu_history.py \
        scripts/tests/test_futu_route_trades.py \
        scripts/tests/test_futu_twr_excludes_deposits.py \
        scripts/tests/test_max_drawdown_uses_running_peak.py
```

Stop.

---

### Task 20: `GET /equity-curve` cross-broker

**Files:**

- Create: `src/xenon/api/routes/equity_curve_cross_broker.py`
- Modify: `src/xenon/api/server.py` (register router)
- Test: `scripts/tests/test_equity_curve_cross_broker.py`

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_equity_curve_cross_broker.py
from datetime import date
from decimal import Decimal
from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import write_nav_row
from sqlalchemy import text


def _seed_ib_nav(d: date, nav: float):
    with get_sync_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO xenon.nav_history
                (broker, account_env, broker_account, date, nav, source,
                 total, cash, stock_value, options_value)
            VALUES ('IB', 'paper', 'pytest', :d, :nav, 'reported',
                    :nav, 0, :nav, 0)
            ON CONFLICT DO NOTHING
        """), {"d": d, "nav": nav})


def test_per_broker_keys(clean_scope, futu_pytest_app_scope):
    futu_scope = AccountScope("FUTU", "paper", "pytest")
    write_nav_row(futu_scope, date(2024, 5, 1), Decimal("1000"),
                  "reconstructed", Decimal("1000"), Decimal("0"))
    # `futu_pytest_app_scope` pins both IB and FUTU broker_accounts to
    # "pytest" so the seeded IB row resolves cleanly via get_account_scope.
    _seed_ib_nav(date(2024, 5, 1), 2000.0)

    with TestClient(app) as client:
        r = client.get("/equity-curve",
                       params={"brokers": "IB,FUTU", "combined": "true"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["per_broker"].keys()) == {"IB", "FUTU"}
    # Combined renders only for dates with both brokers populated.
    assert body["combined"][0]["nav"] == 3000.0
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest scripts/tests/test_equity_curve_cross_broker.py -xvs
```

- [ ] **Step 3: Implement**

```python
# src/xenon/api/routes/equity_curve_cross_broker.py
"""Cross-broker equity curve: IB + Futu lines, optional combined sum.

C30: real IB account IDs (DU0000000, U1234567) and Futu account IDs differ.
Resolve scope PER BROKER — `get_account_scope` for IB,
`get_futu_account_scope` for FUTU — instead of filtering both with the same
`broker_account` (which only worked in tests because both seeds used
`pytest`).
"""
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text

from xenon.api.guards import get_account_scope, get_futu_account_scope
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

router = APIRouter(tags=["equity_curve"])


@router.get("/equity-curve")
def equity_curve_cross_broker(
    request: Request,
    brokers: str = Query("IB,FUTU"),
    combined: bool = False,
) -> dict:
    broker_list = [b.strip().upper() for b in brokers.split(",")
                   if b.strip()]
    allowed = {"IB", "FUTU"}
    bad = set(broker_list) - allowed
    if bad:
        raise HTTPException(status_code=422, detail={
            "reason_code": "broker_invalid", "values": list(bad)})

    # C30: resolve scope per broker so the FUTU side reads its own
    # broker_account.
    ib_scope = get_account_scope(request)
    futu_scope = get_futu_account_scope(request)
    scopes_by_broker = {"IB": ib_scope, "FUTU": futu_scope}

    per_broker: dict[str, list[dict]] = {}
    with get_sync_engine().connect() as conn:
        for broker in broker_list:
            sc = scopes_by_broker[broker]
            rows = conn.execute(text("""
                SELECT date, nav, source FROM xenon.nav_history
                 WHERE broker=:broker
                   AND account_env=:env AND broker_account=:acc
                 ORDER BY date
            """), {"broker": broker,
                   "env": sc.account_env,
                   "acc": sc.broker_account}).fetchall()
            per_broker[broker] = [{
                "date": r.date.isoformat(),
                "nav": float(r.nav),
                "source": r.source,
            } for r in rows]

    combined_series: list[dict] | None = None
    if combined and len(per_broker) >= 2:
        per_date: dict[str, dict[str, float]] = {}
        for broker, rows in per_broker.items():
            for r in rows:
                per_date.setdefault(r["date"], {})[broker] = r["nav"]
        combined_series = [
            {"date": d, "nav": sum(per_date[d][b] for b in broker_list)}
            for d in sorted(per_date)
            if all(b in per_date[d] for b in broker_list)
        ]

    return {"per_broker": per_broker, "combined": combined_series}
```

Register the router in `src/xenon/api/server.py`:

```python
from xenon.api.routes import equity_curve_cross_broker as eqc_routes
app.include_router(eqc_routes.router)
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest scripts/tests/test_equity_curve_cross_broker.py -xvs
```

- [ ] **Step 5: Stage**

```bash
git add src/xenon/api/routes/equity_curve_cross_broker.py \
        src/xenon/api/server.py \
        scripts/tests/test_equity_curve_cross_broker.py
```

Stop.

---

## Milestone 8 — Frontend (Tasks 21-24)

### Task 21: TypeScript types + Next.js API proxy routes + data hooks

**Files:**

- Create: `web/lib/futuHistory/types.ts`
- Create: `web/app/api/futu/equity-curve/route.ts`
- Create: `web/app/api/futu/trades/route.ts`
- Create: `web/app/api/futu/performance/route.ts`
- Create: `web/app/api/futu/reconciliation/route.ts`
- Create: `web/app/api/futu/history/sync/route.ts`
- Create: `web/app/api/equity-curve/route.ts`
- Create: `web/lib/useFutuHistory.ts`
- Create: `web/lib/useEquityCurve.ts`
- Test: `web/tests/useFutuHistory.test.tsx` (C15 — JSX inside hook wrapper requires `.tsx`)

- [ ] **Step 1: Write the hook test**

```typescript
// web/tests/useFutuHistory.test.tsx
// C15: file is `.tsx` not `.ts` because the wrapper below returns JSX
// (`QueryClientProvider`). `tsc --noEmit` rejects JSX inside `.ts`.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useFutuEquityCurve, useFutuTrades, useFutuPerformance,
         useFutuReconciliation, useFutuSync } from "@/lib/useFutuHistory";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  global.fetch = vi.fn();
});

describe("useFutuEquityCurve", () => {
  it("hits /api/futu/equity-curve with from/to query", async () => {
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => [{ date: "2024-05-01", nav: 1000,
                           source: "reconstructed", daily_pnl: 0,
                           recon: null }],
    });
    const { result } = renderHook(
      () => useFutuEquityCurve({ from: "2024-05-01", to: "2024-05-01" }),
      { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/futu/equity-curve"));
    expect(result.current.data?.[0].source).toBe("reconstructed");
  });
});

describe("useFutuSync", () => {
  it("POSTs to /api/futu/history/sync", async () => {
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", fills_added: 3,
                           cash_flows_added: 0, nav_rows_added: 1,
                           warnings: [] }),
    });
    const { result } = renderHook(() => useFutuSync(), { wrapper });
    await result.current.mutateAsync({ since: "auto" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/futu/history/sync",
      expect.objectContaining({ method: "POST" }));
  });
});
```

- [ ] **Step 2: Run test to confirm failure**

```bash
cd web && npm test -- useFutuHistory
```

- [ ] **Step 3: Implement types**

```typescript
// web/lib/futuHistory/types.ts

export type NavSource = "reconstructed" | "reported";

export interface EquityCurvePoint {
  date: string;
  nav: number;
  source: NavSource;
  daily_pnl: number | null;
  recon: {
    delta: number;
    exceeded: boolean;
    notes: Record<string, unknown> | null;
  } | null;
}

export interface FutuTradeRow {
  futu_deal_id: string;
  ticker: string;
  action: "BUY" | "SELL";
  quantity: number;
  price: number;
  fees: number;
  realized_pnl: number | null;
  filled_at: string;
}

export interface FutuTradesPage {
  rows: FutuTradeRow[];
  next_cursor: string | null;
}

export interface FutuPerformance {
  total_return_twr: number;
  cagr: number;
  max_drawdown: number;
  win_rate: number | null;
  avg_holding_days: number | null;
  best_trade: unknown | null;
  worst_trade: unknown | null;
  total_commissions: number | null;
  monthly_pnl: Record<string, number>;
  external_cash_flows: { deposits: number; withdrawals: number };
}

export interface FutuReconciliationRow {
  kind: "daily" | "anomaly";
  recon_date: string;
  reported_nav: number | null;
  reconstructed_nav: number | null;
  delta: number | null;
  threshold: number | null;
  exceeded: boolean | null;
  notes: Record<string, unknown> | null;
  computed_at: string;
}

export interface FutuReconciliation {
  current_delta: number | null;
  rows: FutuReconciliationRow[];
}

export interface FutuSyncResult {
  status: string;
  fills_added: number;
  cash_flows_added: number;
  nav_rows_added: number;
  warnings: string[];
}

export interface CrossBrokerEquityCurve {
  per_broker: Record<
    "IB" | "FUTU",
    { date: string; nav: number; source: NavSource }[]
  >;
  combined: { date: string; nav: number }[] | null;
}
```

- [ ] **Step 4: Implement Next.js proxy routes**

Each route forwards to FastAPI via `xenonFetch()`. Follow the existing `web/app/api/futu/portfolio/route.ts` pattern.

```typescript
// web/app/api/futu/equity-curve/route.ts
import { NextRequest } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function GET(req: NextRequest) {
  const u = new URL(req.url);
  return xenonFetch("/futu/equity-curve" + u.search, { method: "GET" });
}
```

```typescript
// web/app/api/futu/trades/route.ts
import { NextRequest } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function GET(req: NextRequest) {
  const u = new URL(req.url);
  return xenonFetch("/futu/trades" + u.search, { method: "GET" });
}
```

```typescript
// web/app/api/futu/performance/route.ts
import { NextRequest } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function GET(req: NextRequest) {
  const u = new URL(req.url);
  return xenonFetch("/futu/performance" + u.search, { method: "GET" });
}
```

```typescript
// web/app/api/futu/reconciliation/route.ts
import { xenonFetch } from "@/lib/xenonApi";

export async function GET() {
  return xenonFetch("/futu/reconciliation", { method: "GET" });
}
```

```typescript
// web/app/api/futu/history/sync/route.ts
import { NextRequest } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function POST(req: NextRequest) {
  const body = await req.text();
  return xenonFetch("/futu/history/sync", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
}
```

```typescript
// web/app/api/equity-curve/route.ts
import { NextRequest } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function GET(req: NextRequest) {
  const u = new URL(req.url);
  return xenonFetch("/equity-curve" + u.search, { method: "GET" });
}
```

- [ ] **Step 5: Implement the hooks**

```typescript
// web/lib/useFutuHistory.ts
"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type {
  EquityCurvePoint,
  FutuTradesPage,
  FutuPerformance,
  FutuReconciliation,
  FutuSyncResult,
} from "@/lib/futuHistory/types";

const j = async <T>(url: string): Promise<T> => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json() as Promise<T>;
};

export function useFutuEquityCurve(range: { from: string; to: string }) {
  return useQuery({
    queryKey: ["futu", "equity-curve", range],
    queryFn: () =>
      j<EquityCurvePoint[]>(
        `/api/futu/equity-curve?from=${range.from}&to=${range.to}`,
      ),
    staleTime: 5 * 60_000,
  });
}

export interface TradesQuery {
  from: string;
  to: string;
  ticker?: string;
  action?: "BUY" | "SELL";
  sort?: "filled_at" | "realized_pnl" | "ticker" | "fees";
  direction?: "asc" | "desc";
  limit?: number;
  cursor?: string;
}

export function useFutuTrades(q: TradesQuery) {
  const params = new URLSearchParams(
    Object.entries(q)
      .filter(([, v]) => v !== undefined)
      .map(([k, v]) => [k, String(v)]) as [string, string][],
  );
  return useQuery({
    queryKey: ["futu", "trades", q],
    queryFn: () => j<FutuTradesPage>(`/api/futu/trades?${params}`),
    staleTime: 60_000,
  });
}

export function useFutuPerformance(range: { from: string; to: string }) {
  return useQuery({
    queryKey: ["futu", "performance", range],
    queryFn: () =>
      j<FutuPerformance>(
        `/api/futu/performance?from=${range.from}&to=${range.to}`,
      ),
    staleTime: 5 * 60_000,
  });
}

export function useFutuReconciliation() {
  return useQuery({
    queryKey: ["futu", "reconciliation"],
    queryFn: () => j<FutuReconciliation>("/api/futu/reconciliation"),
    refetchInterval: 5 * 60_000,
  });
}

export function useFutuSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      since?: string;
      rematch?: boolean;
    }): Promise<FutuSyncResult> => {
      const r = await fetch("/api/futu/history/sync", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          since: body.since ?? "auto",
          rematch: body.rematch ?? false,
        }),
      });
      if (!r.ok) throw new Error(`sync failed: ${r.status}`);
      return r.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["futu"] });
    },
  });
}
```

```typescript
// web/lib/useEquityCurve.ts
"use client";
import { useQuery } from "@tanstack/react-query";
import type { CrossBrokerEquityCurve } from "@/lib/futuHistory/types";

export function useEquityCurveCrossBroker(
  opts: {
    brokers?: ("IB" | "FUTU")[];
    combined?: boolean;
  } = {},
) {
  const brokers = (opts.brokers ?? ["IB", "FUTU"]).join(",");
  const combined = opts.combined ? "true" : "false";
  return useQuery({
    queryKey: ["equity-curve", brokers, combined],
    queryFn: async (): Promise<CrossBrokerEquityCurve> => {
      const r = await fetch(
        `/api/equity-curve?brokers=${brokers}&combined=${combined}`,
      );
      if (!r.ok) throw new Error(`equity-curve: ${r.status}`);
      return r.json();
    },
    staleTime: 5 * 60_000,
  });
}
```

- [ ] **Step 6: Run tests to confirm pass**

```bash
cd web && npm test -- useFutuHistory
```

- [ ] **Step 7: Stage**

```bash
git add web/lib/futuHistory/types.ts \
        web/app/api/futu/equity-curve/route.ts \
        web/app/api/futu/trades/route.ts \
        web/app/api/futu/performance/route.ts \
        web/app/api/futu/reconciliation/route.ts \
        web/app/api/futu/history/sync/route.ts \
        web/app/api/equity-curve/route.ts \
        web/lib/useFutuHistory.ts \
        web/lib/useEquityCurve.ts \
        web/tests/useFutuHistory.test.tsx
```

Stop.

---

### Task 22: `<FutuEquityCurve />` component

**Files:**

- Create: `web/components/futu/FutuEquityCurve.tsx`
- Test: `web/tests/futu-equity-curve.test.tsx`

- [ ] **Step 1: Write the test**

```tsx
// web/tests/futu-equity-curve.test.tsx
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { FutuEquityCurve } from "@/components/futu/FutuEquityCurve";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => [
      {
        date: "2024-05-01",
        nav: 10000,
        source: "reconstructed",
        daily_pnl: 0,
        recon: null,
      },
      {
        date: "2024-05-02",
        nav: 10100,
        source: "reported",
        daily_pnl: 100,
        recon: { delta: 50, exceeded: false, notes: null },
      },
    ],
  });
});

describe("<FutuEquityCurve />", () => {
  it("renders the chart container", async () => {
    render(wrap(<FutuEquityCurve from="2024-05-01" to="2024-05-02" />));
    expect(await screen.findByTestId("futu-equity-curve")).toBeInTheDocument();
  });

  it("renders a reconstructed-segment badge in legend", async () => {
    render(wrap(<FutuEquityCurve from="2024-05-01" to="2024-05-02" />));
    expect(await screen.findByText(/reconstructed/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to confirm failure**

```bash
cd web && npm test -- futu-equity-curve
```

- [ ] **Step 3: Implement**

C13 + C37: the project standard is `d3` + `ChartPanel` (see
`web/components/InternalsSkewChart.tsx` and `web/components/PerformancePanel.tsx`).
`recharts` is NOT in `web/package.json`. `ChartPanel` requires the
`family: ChartFamily` prop and brand tokens for stroke colors per
`web/CLAUDE.md`. The component below renders the reconstructed prefix
(dim violet) + reported suffix (full violet) as two SVG paths inside the
ChartPanel envelope.

```tsx
// web/components/futu/FutuEquityCurve.tsx
"use client";
import { useEffect, useRef } from "react";
import * as d3 from "d3";

import { useFutuEquityCurve } from "@/lib/useFutuHistory";
import ChartPanel from "@/components/charts/ChartPanel";

interface Props {
  from: string;
  to: string;
}

export function FutuEquityCurve({ from, to }: Props) {
  const { data, isLoading, error } = useFutuEquityCurve({ from, to });
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!data || !svgRef.current) return;
    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const w = svgRef.current.clientWidth;
    const h = 240;
    const m = { top: 10, right: 12, bottom: 22, left: 48 };
    const x = d3
      .scaleUtc()
      .domain(d3.extent(data, (d) => new Date(d.date)) as [Date, Date])
      .range([m.left, w - m.right]);
    const y = d3
      .scaleLinear()
      .domain([
        d3.min(data, (d) => d.nav) ?? 0,
        d3.max(data, (d) => d.nav) ?? 1,
      ])
      .nice()
      .range([h - m.bottom, m.top]);
    const line = d3
      .line<(typeof data)[number]>()
      .x((d) => x(new Date(d.date)))
      .y((d) => y(d.nav));
    svg
      .append("g")
      .attr("transform", `translate(0,${h - m.bottom})`)
      .call(d3.axisBottom(x).ticks(6))
      .attr("color", "var(--brand-text-muted)");
    svg
      .append("g")
      .attr("transform", `translate(${m.left},0)`)
      .call(d3.axisLeft(y).ticks(5))
      .attr("color", "var(--brand-text-muted)");

    // Split into reconstructed prefix + reported suffix; render two paths.
    const idx = data.findIndex((d) => d.source === "reported");
    const reconstructed = idx === -1 ? data : data.slice(0, idx + 1);
    const reported = idx === -1 ? [] : data.slice(idx);
    if (reconstructed.length > 1) {
      svg
        .append("path")
        .datum(reconstructed)
        .attr("fill", "none")
        .attr("stroke", "var(--brand-accent-violet)")
        .attr("stroke-opacity", 0.45)
        .attr("stroke-width", 1.5)
        .attr("d", line);
    }
    if (reported.length > 1) {
      svg
        .append("path")
        .datum(reported)
        .attr("fill", "none")
        .attr("stroke", "var(--brand-accent-violet)")
        .attr("stroke-width", 1.8)
        .attr("d", line);
    }
  }, [data]);

  if (isLoading) return <div data-testid="futu-equity-curve-loading" />;
  if (error || !data)
    return <div data-testid="futu-equity-curve-error">Failed to load.</div>;

  return (
    <ChartPanel
      family="analytical-time-series"
      title="Futu equity curve"
      legend={[
        {
          label: "reconstructed",
          color: "var(--brand-accent-violet)",
        },
        {
          label: "reported",
          color: "var(--brand-accent-violet)",
        },
      ]}
      dataTestId="futu-equity-curve-panel"
    >
      <svg
        ref={svgRef}
        data-testid="futu-equity-curve"
        className="w-full h-60 block"
      />
    </ChartPanel>
  );
}
```

Brand-token notes (C37): if `--brand-accent-violet` / `--brand-text-muted`
are not yet exposed in `brand/tokens`, audit `brand/CLAUDE.md` and add the
violet/cyan/amber/muted-text variables there (separate small task). The d3
axis/path strokes accept any CSS color including `var(--token)`. Drop
`rounded-md`/`rounded-full`; the brand spec is 4px max border-radius (use
`rounded-sm` for inputs/buttons).

- [ ] **Step 4: Run test to confirm pass**

```bash
cd web && npm test -- futu-equity-curve
```

- [ ] **Step 5: Stage**

```bash
git add web/components/futu/FutuEquityCurve.tsx \
        web/tests/futu-equity-curve.test.tsx
```

Stop.

---

### Task 23: `<FutuTradeHistoryTable />` + `<FutuPerformanceStats />`

**Files:**

- Create: `web/components/futu/FutuTradeHistoryTable.tsx`
- Create: `web/components/futu/FutuPerformanceStats.tsx`
- Test: `web/tests/futu-trade-history-table.test.tsx`
- Test: `web/tests/futu-performance-stats.test.tsx`

- [ ] **Step 1: Write the tests**

```tsx
// web/tests/futu-trade-history-table.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { FutuTradeHistoryTable } from "@/components/futu/FutuTradeHistoryTable";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      rows: [
        {
          futu_deal_id: "1",
          ticker: "AAPL",
          action: "BUY",
          quantity: 100,
          price: 150,
          fees: 1,
          realized_pnl: null,
          filled_at: "2024-05-01T10:00:00Z",
        },
        {
          futu_deal_id: "2",
          ticker: "AAPL",
          action: "SELL",
          quantity: 100,
          price: 160,
          fees: 1,
          realized_pnl: 998,
          filled_at: "2024-05-02T10:00:00Z",
        },
      ],
      next_cursor: null,
    }),
  });
});

describe("<FutuTradeHistoryTable />", () => {
  it("renders header cells", async () => {
    render(wrap(<FutuTradeHistoryTable from="2024-05-01" to="2024-05-02" />));
    expect(await screen.findByText("Ticker")).toBeInTheDocument();
    expect(screen.getByText("Realized P&L")).toBeInTheDocument();
  });

  it("renders fetched rows", async () => {
    render(wrap(<FutuTradeHistoryTable from="2024-05-01" to="2024-05-02" />));
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("$998.00")).toBeInTheDocument();
  });
});
```

```tsx
// web/tests/futu-performance-stats.test.tsx
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { FutuPerformanceStats } from "@/components/futu/FutuPerformanceStats";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      total_return_twr: 0.082,
      cagr: 0.165,
      max_drawdown: -0.07,
      win_rate: 0.55,
      avg_holding_days: 12,
      best_trade: null,
      worst_trade: null,
      total_commissions: 145.5,
      monthly_pnl: {},
      external_cash_flows: { deposits: 1000, withdrawals: 0 },
    }),
  });
});

describe("<FutuPerformanceStats />", () => {
  it("renders TWR card", async () => {
    render(wrap(<FutuPerformanceStats from="2024-05-01" to="2024-06-01" />));
    expect(await screen.findByText("TWR")).toBeInTheDocument();
    expect(screen.getByText(/8\.20%/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
cd web && npm test -- futu-trade-history-table futu-performance-stats
```

- [ ] **Step 3: Implement the table**

```tsx
// web/components/futu/FutuTradeHistoryTable.tsx
"use client";
import { useState } from "react";
import { useFutuTrades } from "@/lib/useFutuHistory";

interface Props {
  from: string;
  to: string;
}

export function FutuTradeHistoryTable({ from, to }: Props) {
  const [ticker, setTicker] = useState("");
  const [action, setAction] = useState<"" | "BUY" | "SELL">("");
  const [cursor, setCursor] = useState<string | undefined>();
  const { data, isLoading } = useFutuTrades({
    from,
    to,
    ticker: ticker || undefined,
    action: action || undefined,
    sort: "filled_at",
    direction: "desc",
    limit: 100,
    cursor,
  });

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950">
      <div className="flex gap-2 p-3 border-b border-zinc-800">
        <input
          className="bg-zinc-900 text-sm px-2 py-1 rounded"
          placeholder="Ticker"
          value={ticker}
          onChange={(e) => {
            setTicker(e.target.value.toUpperCase());
            setCursor(undefined);
          }}
        />
        <select
          className="bg-zinc-900 text-sm px-2 py-1 rounded"
          value={action}
          onChange={(e) => {
            setAction(e.target.value as any);
            setCursor(undefined);
          }}
        >
          <option value="">All actions</option>
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
      </div>
      <table className="w-full text-xs">
        <thead className="text-zinc-500">
          <tr>
            <th className="text-left p-2">Filled</th>
            <th className="text-left p-2">Ticker</th>
            <th className="text-left p-2">Action</th>
            <th className="text-right p-2">Qty</th>
            <th className="text-right p-2">Price</th>
            <th className="text-right p-2">Fees</th>
            <th className="text-right p-2">Realized P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {isLoading && (
            <tr>
              <td className="p-2" colSpan={7}>
                Loading…
              </td>
            </tr>
          )}
          {data?.rows.map((r) => (
            <tr key={r.futu_deal_id} className="border-t border-zinc-900">
              <td className="p-2">{r.filled_at.slice(0, 19)}</td>
              <td className="p-2">{r.ticker}</td>
              <td
                className={`p-2 ${
                  r.action === "BUY" ? "text-emerald-400" : "text-amber-400"
                }`}
              >
                {r.action}
              </td>
              <td className="p-2 text-right">{r.quantity}</td>
              <td className="p-2 text-right">${r.price.toFixed(2)}</td>
              <td className="p-2 text-right">${r.fees.toFixed(2)}</td>
              <td
                className={`p-2 text-right ${
                  r.realized_pnl == null
                    ? "text-zinc-500"
                    : r.realized_pnl >= 0
                      ? "text-emerald-400"
                      : "text-rose-400"
                }`}
              >
                {r.realized_pnl == null ? "—" : `$${r.realized_pnl.toFixed(2)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data?.next_cursor && (
        <div className="p-3 border-t border-zinc-800">
          <button
            className="text-xs text-violet-400"
            onClick={() => setCursor(data.next_cursor!)}
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Implement the stats strip**

```tsx
// web/components/futu/FutuPerformanceStats.tsx
"use client";
import { useFutuPerformance } from "@/lib/useFutuHistory";

const fmtPct = (n: number) => `${(n * 100).toFixed(2)}%`;
const fmtUSD = (n: number) =>
  `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="text-[10px] uppercase text-zinc-500">{label}</div>
      <div className="text-sm font-medium">{value}</div>
    </div>
  );
}

export function FutuPerformanceStats({
  from,
  to,
}: {
  from: string;
  to: string;
}) {
  const { data, isLoading } = useFutuPerformance({ from, to });
  if (isLoading || !data)
    return <div className="text-xs text-zinc-500">Loading…</div>;
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
      <Card label="TWR" value={fmtPct(data.total_return_twr)} />
      <Card label="CAGR" value={fmtPct(data.cagr)} />
      <Card label="Max DD" value={fmtPct(data.max_drawdown)} />
      <Card
        label="Win rate"
        value={data.win_rate != null ? fmtPct(data.win_rate) : "—"}
      />
      <Card
        label="Avg holding"
        value={
          data.avg_holding_days != null
            ? `${data.avg_holding_days.toFixed(0)}d`
            : "—"
        }
      />
      <Card
        label="Commissions"
        value={
          data.total_commissions != null ? fmtUSD(data.total_commissions) : "—"
        }
      />
      <Card
        label="Deposits"
        value={fmtUSD(data.external_cash_flows.deposits)}
      />
      <Card
        label="Withdrawals"
        value={fmtUSD(data.external_cash_flows.withdrawals)}
      />
    </div>
  );
}
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
cd web && npm test -- futu-trade-history-table futu-performance-stats
```

- [ ] **Step 6: Stage**

```bash
git add web/components/futu/FutuTradeHistoryTable.tsx \
        web/components/futu/FutuPerformanceStats.tsx \
        web/tests/futu-trade-history-table.test.tsx \
        web/tests/futu-performance-stats.test.tsx
```

Stop.

---

### Task 24: `<EquityCurveCrossBroker />` + wire into Futu tab

**Files:**

- Create: `web/components/EquityCurveCrossBroker.tsx`
- Modify: `web/components/AccountTabBar.tsx` or `web/components/WorkspaceShell.tsx` (locate Futu tab; mount Futu components; add cross-broker chart at a workspace position visible regardless of broker tab)
- Test: `web/tests/equity-curve-cross-broker.test.tsx`

- [ ] **Step 1: Write the test**

```tsx
// web/tests/equity-curve-cross-broker.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { EquityCurveCrossBroker } from "@/components/EquityCurveCrossBroker";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      per_broker: {
        IB: [{ date: "2024-05-01", nav: 2000, source: "reported" }],
        FUTU: [{ date: "2024-05-01", nav: 1000, source: "reconstructed" }],
      },
      combined: null,
    }),
  });
});

describe("<EquityCurveCrossBroker />", () => {
  it("renders two-line chart", async () => {
    render(wrap(<EquityCurveCrossBroker />));
    expect(
      await screen.findByTestId("equity-curve-cross-broker"),
    ).toBeInTheDocument();
  });

  it("toggle Combined NAV refetches with combined=true", async () => {
    render(wrap(<EquityCurveCrossBroker />));
    const toggle = await screen.findByLabelText(/combined/i);
    fireEvent.click(toggle);
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("combined=true"),
    );
  });
});
```

- [ ] **Step 2: Run test to confirm failure**

```bash
cd web && npm test -- equity-curve-cross-broker
```

- [ ] **Step 3: Implement**

C13 + C37: d3 + `ChartPanel` (no recharts). Render IB + FUTU as separate
SVG paths plus a third optional Combined path; brand-token strokes; data-
testid preserved for the Vitest selectors.

```tsx
// web/components/EquityCurveCrossBroker.tsx
"use client";
import { useEffect, useRef, useState } from "react";
import * as d3 from "d3";

import { useEquityCurveCrossBroker } from "@/lib/useEquityCurve";
import ChartPanel from "@/components/charts/ChartPanel";

type Point = {
  date: string;
  IB: number | null;
  FUTU: number | null;
  Combined: number | null;
};

export function EquityCurveCrossBroker() {
  const [combined, setCombined] = useState(false);
  const { data, isLoading } = useEquityCurveCrossBroker({ combined });
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!data || !svgRef.current) return;
    const dates = Array.from(
      new Set([
        ...(data.per_broker.IB?.map((r) => r.date) ?? []),
        ...(data.per_broker.FUTU?.map((r) => r.date) ?? []),
        ...(data.combined?.map((r) => r.date) ?? []),
      ]),
    ).sort();
    const ibByDate = new Map(
      (data.per_broker.IB ?? []).map((r) => [r.date, r.nav]),
    );
    const fuByDate = new Map(
      (data.per_broker.FUTU ?? []).map((r) => [r.date, r.nav]),
    );
    const coByDate = new Map((data.combined ?? []).map((r) => [r.date, r.nav]));
    const points: Point[] = dates.map((d) => ({
      date: d,
      IB: ibByDate.get(d) ?? null,
      FUTU: fuByDate.get(d) ?? null,
      Combined: coByDate.get(d) ?? null,
    }));

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const w = svgRef.current.clientWidth;
    const h = 240;
    const m = { top: 10, right: 12, bottom: 22, left: 48 };

    const allNavs = points.flatMap((p) =>
      [p.IB, p.FUTU, p.Combined].filter((v): v is number => v != null),
    );
    const x = d3
      .scaleUtc()
      .domain(d3.extent(points, (p) => new Date(p.date)) as [Date, Date])
      .range([m.left, w - m.right]);
    const y = d3
      .scaleLinear()
      .domain([d3.min(allNavs) ?? 0, d3.max(allNavs) ?? 1])
      .nice()
      .range([h - m.bottom, m.top]);

    svg
      .append("g")
      .attr("transform", `translate(0,${h - m.bottom})`)
      .call(d3.axisBottom(x).ticks(6))
      .attr("color", "var(--brand-text-muted)");
    svg
      .append("g")
      .attr("transform", `translate(${m.left},0)`)
      .call(d3.axisLeft(y).ticks(5))
      .attr("color", "var(--brand-text-muted)");

    const drawLine = (
      key: "IB" | "FUTU" | "Combined",
      color: string,
      width: number,
    ) => {
      const series = points
        .filter((p) => p[key] != null)
        .map((p) => ({ d: new Date(p.date), v: p[key] as number }));
      if (series.length < 2) return;
      const line = d3
        .line<{ d: Date; v: number }>()
        .x((s) => x(s.d))
        .y((s) => y(s.v));
      svg
        .append("path")
        .datum(series)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-width", width)
        .attr("d", line);
    };
    drawLine("IB", "var(--brand-accent-cyan)", 1.6);
    drawLine("FUTU", "var(--brand-accent-violet)", 1.6);
    if (combined) drawLine("Combined", "var(--brand-accent-amber)", 1.8);
  }, [data, combined]);

  if (isLoading || !data) return <div data-testid="ecb-loading" />;

  return (
    <ChartPanel
      family="analytical-time-series"
      title="Equity curve · cross-broker"
      legend={[
        { label: "IB", color: "var(--brand-accent-cyan)" },
        { label: "FUTU", color: "var(--brand-accent-violet)" },
        ...(combined
          ? [{ label: "Combined", color: "var(--brand-accent-amber)" }]
          : []),
      ]}
      dataTestId="equity-curve-cross-broker-panel"
    >
      <div className="flex items-center justify-end pb-2">
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={combined}
            onChange={(e) => setCombined(e.target.checked)}
          />
          Combined NAV
        </label>
      </div>
      <svg
        ref={svgRef}
        data-testid="equity-curve-cross-broker"
        className="w-full h-60 block"
      />
    </ChartPanel>
  );
}
```

- [ ] **Step 4: Wire components into the Futu tab**

Locate the Futu account tab in `web/components/AccountTabBar.tsx` (or wherever the existing `<FutuPortfolio />`-like panel mounts). Add a panel that composes the new components when the active broker tab is Futu:

```tsx
// web/components/futu/FutuHistoryPanel.tsx
"use client";
import { FutuEquityCurve } from "./FutuEquityCurve";
import { FutuPerformanceStats } from "./FutuPerformanceStats";
import { FutuTradeHistoryTable } from "./FutuTradeHistoryTable";
import { useFutuSync, useFutuReconciliation } from "@/lib/useFutuHistory";

const START = "2024-05-01";

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

export function FutuHistoryPanel() {
  const to = todayISO();
  const recon = useFutuReconciliation();
  const sync = useFutuSync();

  return (
    <div className="space-y-4">
      {recon.data?.current_delta != null &&
        Math.abs(recon.data.current_delta) > 25 && (
          <div
            className="rounded-md bg-orange-950/40 border border-orange-900
                        text-orange-300 text-xs px-3 py-2"
          >
            Reconciliation mismatch: ${recon.data.current_delta.toFixed(2)}
          </div>
        )}
      <div className="flex justify-end">
        <button
          className="text-xs px-3 py-1 rounded bg-violet-600/20
                     border border-violet-600/40 text-violet-300"
          disabled={sync.isPending}
          onClick={() => sync.mutate({ since: "auto" })}
        >
          {sync.isPending ? "Syncing…" : "Sync now"}
        </button>
      </div>
      <FutuPerformanceStats from={START} to={to} />
      <FutuEquityCurve from={START} to={to} />
      <FutuTradeHistoryTable from={START} to={to} />
    </div>
  );
}
```

In the existing Futu panel render path (search `useFutuPortfolio` to find it), add `<FutuHistoryPanel />` below the live positions view. Above the broker tabs (cross-broker workspace position), mount `<EquityCurveCrossBroker />`.

- [ ] **Step 5: Run tests + manual browser verification**

```bash
cd web && npm test -- equity-curve-cross-broker
cd /Users/chenxi/projects/xenon && scripts/infra/dev.sh paper
# In a separate shell, open http://localhost:3000, switch to the Futu tab,
# confirm the new panel renders + Sync-now button fires the POST.
```

Per xenon CLAUDE.md mandatory rule: no UI change is done until visually confirmed in the browser.

- [ ] **Step 6: Stage**

```bash
git add web/components/EquityCurveCrossBroker.tsx \
        web/components/futu/FutuHistoryPanel.tsx \
        web/components/AccountTabBar.tsx \
        web/tests/equity-curve-cross-broker.test.tsx
```

Stop.

---

## Milestone 9 — E2E + CI guards (Tasks 25-26)

### Task 25: Playwright E2E spec

**Files:**

- Create: `web/e2e/futu-history.spec.ts`

- [ ] **Step 1: Write the spec**

```typescript
// web/e2e/futu-history.spec.ts
import { test, expect } from "@playwright/test";

test.describe("Futu history surfaces", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: /Futu/i }).click();
  });

  test("renders equity curve + trade table + perf stats", async ({ page }) => {
    await expect(page.getByTestId("futu-equity-curve")).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByText("TWR")).toBeVisible();
    await expect(page.getByText("Ticker")).toBeVisible();
  });

  test("sync-now button refreshes data", async ({ page }) => {
    const syncBtn = page.getByRole("button", { name: /sync now/i });
    await syncBtn.click();
    await expect(syncBtn).toHaveText(/syncing/i);
    // Returns to "Sync now" within 30s once the request settles.
    await expect(syncBtn).toHaveText(/sync now/i, { timeout: 30_000 });
  });

  test("cross-broker chart toggles combined", async ({ page }) => {
    await page.goto("/"); // workspace position above tabs
    const chart = page.getByTestId("equity-curve-cross-broker");
    await expect(chart).toBeVisible();
    const toggle = page.getByLabel(/combined/i);
    await toggle.check();
    await expect(toggle).toBeChecked();
  });
});
```

- [ ] **Step 2: Run E2E**

```bash
cd web && npx playwright test futu-history
```

Expected: 3 specs pass. If the dev server isn't running, start it first per Task 24 Step 5.

- [ ] **Step 3: Stage**

```bash
git add web/e2e/futu-history.spec.ts
```

Stop.

---

### Task 26: CI guards — `no_json_fallback_on_futu_history_path.py` + `test_futu_trades_constraint.py` + view test

**Files:**

- Create: `scripts/checks/no_json_fallback_on_futu_history_path.py`
- Modify: `.github/workflows/ci.yml` (add the guard job)
- Test: `scripts/tests/test_futu_trades_constraint.py`
- Test: `scripts/tests/test_all_fills_view.py`

- [ ] **Step 1: Write the trades-constraint test**

```python
# scripts/tests/test_futu_trades_constraint.py
"""xenon.trades retains the IB-only constraint; Futu rows route to futu_trades."""
import pytest
from sqlalchemy import text
from xenon.db.engine import get_sync_engine


def test_trades_ck_broker_is_ib_only():
    with get_sync_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT pg_get_constraintdef(c.oid)
              FROM pg_constraint c
              JOIN pg_class t       ON t.oid = c.conrelid
              JOIN pg_namespace n   ON n.oid = t.relnamespace
             WHERE c.conname = 'ck_trades_broker_ib_only'
               AND n.nspname = 'xenon'
               AND t.relname = 'trades'
        """)).first()
    assert row is not None, "ck_trades_broker_ib_only must still exist"
    assert "'IB'" in row[0]
```

- [ ] **Step 2: Write the all_fills view test**

```python
# scripts/tests/test_all_fills_view.py
"""xenon.all_fills returns fill-grain rows from both IB and Futu."""
from datetime import date, datetime, timezone
from decimal import Decimal
from sqlalchemy import text
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.futu_history import insert_fills


def test_view_returns_futu_rows(clean_scope):
    insert_fills(AccountScope("FUTU", "paper", "pytest"), [dict(
        futu_deal_id="d1", futu_order_id="o", ticker="AAPL",
        futu_code="US.AAPL", market="US", action="BUY", quantity=10,
        price=Decimal("150"), fees=Decimal("1"),
        filled_at=datetime(2024, 5, 1, 10, tzinfo=timezone.utc), raw={})])
    with get_sync_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT broker, ticker, action, quantity, price, fees
              FROM xenon.all_fills
             WHERE broker='FUTU' AND account_env='paper'
               AND broker_account='pytest' AND ticker='AAPL'
        """)).fetchall()
    assert len(rows) == 1
    assert rows[0].action == "BUY"
    assert rows[0].quantity == 10
```

- [ ] **Step 3: Implement the JSON-fallback guard**

```python
# scripts/checks/no_json_fallback_on_futu_history_path.py
"""Fail if any code under the Futu-history surface reintroduces JSON-file
fallback. Patterned after no_json_fallback_on_order_path.py.

Order-path guard does NOT cover Futu. /futu/portfolio retains a legacy
data/futu_portfolio.json read by design (see project_postgres_migration_read_side_gap.md).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WATCHED_PATHS = [
    "src/xenon/api/routes/futu_history.py",
    "src/xenon/api/routes/equity_curve_cross_broker.py",
    "src/xenon/api/services/futu_history.py",
    "web/app/api/futu/equity-curve",
    "web/app/api/futu/trades",
    "web/app/api/futu/performance",
    "web/app/api/futu/reconciliation",
    "web/app/api/futu/history",
    "web/app/api/equity-curve",
]

BANNED = [
    (re.compile(r"readDataFile\("),         "readDataFile() not allowed"),
    (re.compile(r"readFile\(.*futu.*json"), "readFile against data/futu*.json"),
    (re.compile(r"json\.load.*futu.*json"), "json.load against data/futu*.json"),
    (re.compile(r"open\(.*data/futu.*json"), "open() against data/futu*.json"),
]


def walk(p: Path):
    if p.is_file():
        yield p
        return
    for child in p.rglob("*"):
        if child.is_file() and child.suffix in {".py", ".ts", ".tsx"}:
            yield child


def main() -> int:
    failures: list[str] = []
    for rel in WATCHED_PATHS:
        for f in walk(ROOT / rel):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for pattern, reason in BANNED:
                for m in pattern.finditer(text):
                    line = text[:m.start()].count("\n") + 1
                    failures.append(f"{f.relative_to(ROOT)}:{line} — {reason}")
    if failures:
        print("FAIL — JSON-file fallback found on Futu-history path:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"OK — no JSON-file fallback in {len(WATCHED_PATHS)} watched paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add the CI job**

Locate the `order-path-guards` job in `.github/workflows/ci.yml` and append a parallel job:

```yaml
futu-history-guards:
  name: Futu history guards
  runs-on: ubuntu-latest
  needs: [setup]
  steps:
    - uses: actions/checkout@v4
    - name: Set up uv
      uses: astral-sh/setup-uv@v3
    - name: Sync deps
      run: uv sync --frozen --extra test
    - name: No JSON fallback on Futu history path
      run: uv run python scripts/checks/no_json_fallback_on_futu_history_path.py
```

- [ ] **Step 5: Run guard + tests locally**

```bash
uv run python scripts/checks/no_json_fallback_on_futu_history_path.py
uv run pytest scripts/tests/test_futu_trades_constraint.py \
              scripts/tests/test_all_fills_view.py -xvs
```

- [ ] **Step 6: Stage**

```bash
git add scripts/checks/no_json_fallback_on_futu_history_path.py \
        .github/workflows/ci.yml \
        scripts/tests/test_futu_trades_constraint.py \
        scripts/tests/test_all_fills_view.py
```

Stop.

---

## Final integration steps

After Task 26, run the full test suite locally before opening a PR:

```bash
uv run pytest                                                                # full Python suite
cd web && npm run typecheck && npm run lint && npm test                      # full web suite
cd web && npx playwright test futu-history                                   # E2E
uv run python scripts/checks/no_json_fallback_on_futu_history_path.py        # guard
uv run python scripts/checks/no_json_fallback_on_order_path.py               # unchanged guard
```

Verify reads end-to-end against a real Futu account in paper mode (if available) by hitting the new endpoints with `curl`:

```bash
curl -s "http://localhost:8321/futu/equity-curve?from=2024-05-01&to=$(date +%F)" | jq '. | length'
curl -s "http://localhost:8321/futu/reconciliation" | jq .
curl -s -X POST "http://localhost:8321/futu/history/sync" \
     -H 'content-type: application/json' \
     -d '{"since":"auto"}' | jq .
```

Then open a PR (per global CLAUDE.md `gh pr create`); CI runs the full suite + the new guard job. Merge only after CI green and your own visual browser check (chrome-cdp or Playwright) confirms the Futu tab renders correctly.

---

## Self-review log (writer's notes — engineer should re-read before starting)

1. **Spec coverage** — every section in `2026-06-01-futu-account-performance-reconstruction-design.md` is covered: schema (Task 1), FX (Task 3), observability (Task 4), clients (Tasks 5-6), FIFO matcher (Tasks 7-8), DB queries (Task 9), sync orchestration (Tasks 10-14), CLI/launchd (Tasks 15-16), 6 FastAPI routes (Tasks 17-20), frontend (Tasks 21-24), E2E + CI guards (Tasks 25-26).

2. **Type consistency** — names that cross tasks: `Scope` (defined Task 9), `AnchorState` and `_PositionState` (defined Task 14), `FillInput` and `CorpActionInput` (defined Task 7), `FutuApiStats` (defined Task 4), `MIN_HISTORICAL_DATE` (defined Task 10), `convert_to_usd` / `FxRateMissing` (defined Task 3), `LOCK_KEY_FUTU_HISTORY_SYNC` (defined Task 1).

3. **Backwalk gotchas** — the anchor uses `cost_basis_NAV` not `reported_NAV` (Task 14 helpers `_build_anchor_from_live_account` reads `cash` from accinfo + cost-basis positions). `write_nav_row(is_today=False)` makes past dates immutable; only `is_today=True` upserts.

4. **Fee dedup** — order-independent because dedup happens in `sum_cash_flow_for_date` (Task 11), not at ingest. Both `test_fee_then_deal` and `test_deal_then_fee` enforce this.

5. **TZ partitioning** — every date filter against `filled_at` / `flow_at` uses `(filled_at AT TIME ZONE 'America/New_York')::date`. Locked by `test_futu_trade_date_tz_partition.py`.

6. **IB scope** — no IB code is touched. The cross-broker route reads existing `nav_history` rows where `broker='IB'`; no IB-side migration or write. The `xenon.all_fills` view UNIONs `order_fills` (existing) with `futu_trades` (new) — IB join is `WHERE f.broker = 'IB'` against the existing schema.

7. **Standing rules** — `uv` everywhere, no Yahoo Finance (FRED for FX), no commits without explicit user request (each task ends "Stop. Let the user commit."), PR before merge to master.

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-01-futu-account-performance-reconstruction.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each subagent gets a clean context window so the long plan body doesn't crowd out the implementation work.

**2. Inline Execution** — I work through tasks in this session using executing-plans, with checkpoints between milestones for you to review the staged diffs.

**Which approach?**

---

## Review changelog

This plan was reviewed via 3-pass review-cycle (self → Gemini tribunal → Codex tribunal + adversarial) before publication. All 37 findings have been folded into the task bodies above. Summary:

- **C1** — Use existing `AccountScope` instead of duplicate `Scope` dataclass (folded into Tasks 9-20 query module + service + routes)
- **C2** — Use canonical `get_sync_engine()` instead of homegrown `_engine()` lru_cache (Tasks 9-20)
- **C3** — `account_snapshots.account` is NOT NULL — populate from `scope.broker_account` (Task 12 INSERT)
- **C4** — Schema.py uses `xenon_metadata` (not `metadata`); reuse existing SQLAlchemy imports (Task 1 step 4)
- **C5** — Route tests require `futu_pytest_app_scope` fixture override (Task 9 conftest, Tasks 17-20 test signatures)
- **C6** — `clean_scope` fixture extended to wipe `corporate_actions`, `account_snapshots`, `fx_daily` (Task 9 conftest)
- **C7** — Superseded by C18 (don't apply this directly; see C18)
- **C8** — `compute_performance_stats` populates `win_rate`, `avg_holding_days`, `best/worst_trade`, `total_commissions`, `monthly_pnl` (Task 19 service)
- **C9** — Superseded by C13 (recharts not in `web/package.json`)
- **C10** — Wrap advisory-lock unlock in try/except so a stale connection doesn't mask the original error (Task 10 advisory_lock)
- **C11** — Added `GET /futu-stats` route mirroring `/uw-stats` (Task 17)
- **C12** — `parse_since` upper bound uses ET (not UTC) for consistency with all other date-boundary checks (Task 10 service)
- **C13** — Use `d3 + ChartPanel` instead of recharts (Tasks 22, 24 components)
- **C14** — Extend `_TABLES` tuple in `scripts/tests/conftest.py::_truncate_postgres_tables` to cover the new Futu tables (Task 1 step 6b)
- **C15** — Rename `useFutuHistory.test.ts` → `useFutuHistory.test.tsx` (JSX in `.ts` rejected by typecheck) (Task 21)
- **C16** — Guard Decimal division-by-zero in `convert_cash_flow_amount` (Task 11 service)
- **C17** — Close `OpenSecTradeContext` + `OpenQuoteContext` in `finally` so launchd doesn't leak TCP sockets (Task 11 `_run_sync_under_lock`)
- **C18** — `_unapply_buy` quantity direction inverted (was `pos.qty + qty`, must be `pos.qty - qty`); replaces C7 fix (Task 14, plus `test_unapply_directions.py`)
- **C19** — Wire FIFO `lot_history` into `run_backward_walk`; `_rematch_ticker` must request `return_lot_history=True`; module-level `_LOT_HISTORY_CACHE`; new `_unapply_sell_with_history` helper (Tasks 12, 14)
- **C20** — `_row_to_fill` sums ALL fee columns (`commission` + `platform_fee` + `clearing_fee` + …) instead of picking one (Task 5 `_sum_fees`)
- **C21** — Alembic `server_default=sa.text("'X'")` for text columns (no phantom autogenerate diffs) (Task 1 step 5)
- **C22** — Tighten `test_futu_trades_ck_market_us_only` exception to `sqlalchemy.exc.IntegrityError` with `engine.begin()` auto-rollback (Task 1 step 1)
- **C23** — Empirically verify Futu `create_time` time zone + fee column names BEFORE locking `_parse_ny_dt`; added Step 0 probe + `test_futu_dt_parsing.py` (Task 5)
- **C24** — First-sync anchor row is written to `futu_reconciliation_log` (recon-only), NOT `nav_history`, so `write_today_reported_nav` doesn't overwrite it (Task 14 `run_backward_walk`)
- **C25** — `--rematch` actually deletes historical reconstructed `nav_history` rows + clears `_LOT_HISTORY_CACHE` for the scope before re-walking; added `test_futu_rematch_recomputes_historical.py` (Task 14 step 10)
- **C26** — Futu's `accinfo_query` returns `total_assets`, not `net_liquidation`; prefer `FutuClient.fetch_portfolio()` for the anchor builder (Tasks 11, 12, 14)
- **C27** — Call `backfill_fx_series("HKD", …)` + `("CNY", …)` BEFORE `_pull_cash_flow` so non-USD flows don't all degrade to `amount=NULL` on first sync; added `test_fx_backfill_called_before_cash_flow.py` (Task 11)
- **C28** — Pull corp actions BEFORE rematch (spec order 6 → 4 was wrong); `detect_tickers_needing_rematch` also checks `MAX(corporate_actions.effective_date)` (Tasks 12, 13)
- **C29** — Added `get_futu_account_scope` dependency so `/futu/*` routes resolve the FUTU scope in production (not just tests); all FUTU routes use this; cross-broker uses both (Tasks 17-20)
- **C30** — Cross-broker route resolves scope per broker (IB via `get_account_scope`, FUTU via `get_futu_account_scope`) so real-world differing account IDs work (Task 20)
- **C31** — Auto cursor includes a 1-day overlap (`min(cursors) - 1 day`) to catch settlement-date late arrivals; `None` cursors filtered out (Task 11 `_run_sync_under_lock`)
- **C32** — Backwalk fee dedup uses a scope-wide deal_id set (not per-day) to match `sum_cash_flow_for_date`'s `NOT EXISTS` semantics and survive T+1 settlement noise (Task 14 `run_backward_walk`)
- **C33** — `_validate_date` checks upper bound (today ET); added `_validate_range` for inverted from > to (Tasks 18, 19, 20)
- **C34** — Cursor pagination only valid for `sort=filled_at`; other sorts with `cursor` return 422 `cursor_requires_filled_at_sort` (Task 19 `list_trades`)
- **C35** — Max drawdown uses a running peak walking left-to-right (V-shape `100 → 50 → 200` correctly shows −50% DD, not 0%); added `test_max_drawdown_uses_running_peak.py` (Task 19)
- **C36** — `schema.py` `nav_history` Table definition gets the `source` column inline + CheckConstraint (otherwise SQLAlchemy metadata drifts from the DB) (Task 1 step 4)
- **C37** — `ChartPanel` requires `family: ChartFamily` prop (not optional); brand-token CSS variables for stroke colors (no raw hex); 4px max border-radius (Tasks 22, 24)

The original three-pass appendix (with the long-form analyses for each correction) is preserved in the plan's git history at the pre-fold commit. Adversarial operational notes (A1–A5) that did not change code:

- **A1** — `pytest-xdist`: autouse truncate races under `-n auto`; run Futu tests serially or scope each worker to its own test DB.
- **A2** — `insert_fills` / `insert_cash_flows` hold one transaction per batch; fine for <50k rows, document the limit.
- **A3** — First-run backfill on `POST /futu/history/sync` takes 30s+ on a 700-day history (uvicorn/nginx 30-60s timeout). Run the FIRST backfill via the CLI; use the HTTP route only for incremental syncs.
- **A4** — Pre-market sync at 09:00 ET may pull yesterday's close NAV; the 16:30 ET launchd run UPSERTs it correctly. Document expected behavior.
- **A5** — Phase 2 cross-machine launchd: uninstall the dev-Mac job before Phase 2 cutover so the Postgres advisory lock doesn't have to arbitrate.
