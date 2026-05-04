# Broker Account Scope — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable `broker`, `account_env`, `broker_account` columns to all execution/portfolio tables so paper/live data never blends in a shared Postgres, and every row is auditable to a specific broker account.

**Architecture:** Three new columns on 7 tables (order_submissions, trades, wizard_sessions, wizard_combo_attempts, positions, account_snapshots, nav_history). A frozen `AccountScope` dataclass resolves the current scope from `app.state` (FastAPI) or `managedAccounts()` + env (sync callers). All write paths stamp scope; all read/query paths filter by it. Legacy rows get `account_env='legacy_unknown'` — never guessed. Positions and account_snapshots keep their existing `account` column for now (backward compat); new columns are added alongside, callers migrated, `account` dropped in a follow-up.

**Tech Stack:** Python 3.13, SQLAlchemy Core, Alembic, FastAPI, pytest, `uv` for everything.

**Prerequisite:** The `trading_mode` module (merged in `feat/paper-live-mode-switch`) must already be on this branch. It provides `trading_mode.MODE` and `app.state.{trading_mode, account, mode_verified}`.

---

## Naming Conventions

| Column           | Type                                     | Values                                   | Meaning                                     |
| ---------------- | ---------------------------------------- | ---------------------------------------- | ------------------------------------------- |
| `broker`         | `TEXT NOT NULL DEFAULT 'IB'`             | `IB`, `FUTU`                             | Which broker originated this row            |
| `account_env`    | `TEXT NOT NULL DEFAULT 'legacy_unknown'` | `paper`, `live`, `sim`, `legacy_unknown` | Runtime environment at time of write        |
| `broker_account` | `TEXT NOT NULL DEFAULT 'legacy_unknown'` | `DU1234567`, `U9876543`, etc.            | External account identifier from the broker |

**Rules:**

- Execution tables (order_submissions, trades, wizard_sessions, wizard_combo_attempts): `broker` CHECK constrained to `'IB'` only — Futu execution is not permitted.
- Portfolio tables (positions, account_snapshots, nav_history): `broker` CHECK allows both `IB` and `FUTU`.
- All `account_env` checks: `IN ('paper', 'live', 'sim', 'legacy_unknown')`.
- Legacy rows get default values — never backfilled to `paper`/`live` without operator intervention.

---

## File Structure

| File                                                                  | Action                | Responsibility                                                                                      |
| --------------------------------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------- |
| `src/xenon/db/schema.py`                                              | MODIFY                | Add 3 columns + CHECKs to 7 tables; update nav_history PK; update idempotency constraints + indexes |
| `src/xenon/db/migrations/versions/<hash>_add_broker_account_scope.py` | CREATE (autogenerate) | Alembic migration for all schema changes                                                            |
| `src/xenon/execution/account_scope.py`                                | CREATE                | Frozen `AccountScope` dataclass + `resolve_from_app_state()` + `resolve_from_ib()`                  |
| `src/xenon/execution/orders_store.py`                                 | MODIFY                | Accept + persist scope on `reserve_attempt` and `register_from_snapshot`                            |
| `src/xenon/execution/ib_execute.py:317-339`                           | MODIFY                | Pass scope to trade insert                                                                          |
| `src/xenon/execution/ib_sync.py:1094-1117`                            | MODIFY                | Replace hardcoded `account="IB"` with real scope                                                    |
| `src/xenon/execution/single_leg_rehydrate.py:200-208`                 | MODIFY                | Filter `list_unresolved` by current scope                                                           |
| `src/xenon/execution/combo_wizard/rehydrate.py:57-65`                 | MODIFY                | Filter `list_rehydratable` by current scope                                                         |
| `src/xenon/db/queries/orders.py`                                      | MODIFY                | Accept + persist scope on async reserve/query functions                                             |
| `src/xenon/db/queries/portfolio.py`                                   | MODIFY                | Scope `save_positions`, `save_account_snapshot`, `upsert_nav`                                       |
| `src/xenon/db/queries/combo_wizard.py`                                | MODIFY                | Accept scope on `create_session`, `create_attempt`, filter `list_rehydratable`/`list_sessions`      |
| `src/xenon/db/queries/wizard.py`                                      | MODIFY                | Accept scope on async `create_session`, filter queries                                              |
| `src/xenon/db/queries/trades.py`                                      | MODIFY                | Accept scope on `append_trade`                                                                      |
| `src/xenon/api/server.py`                                             | MODIFY                | Build `AccountScope` in lifespan, thread through order routes                                       |
| `src/xenon/api/guards.py`                                             | MODIFY                | Add `get_account_scope()` FastAPI dependency                                                        |
| `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`            | MODIFY                | Filter `list_protected` by scope                                                                    |
| `src/xenon/db/tests/test_account_scope.py`                            | CREATE                | Unit tests for scope resolver                                                                       |
| `src/xenon/db/tests/test_schema_scope.py`                             | CREATE                | Integration: scope columns, constraints, idempotency isolation                                      |

---

## Task 1: Add Scope Columns to Schema + Alembic Migration

**Files:**

- Modify: `src/xenon/db/schema.py`
- Create: Alembic migration (autogenerated)
- Create: `src/xenon/db/tests/test_schema_scope.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/db/tests/test_schema_scope.py`:

```python
"""Verify broker/account_env/broker_account columns exist on scoped tables."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from xenon.db.engine import get_sync_engine
from xenon.db.schema import (
    account_snapshots,
    nav_history,
    order_submissions,
    positions,
    trades,
    wizard_combo_attempts,
    wizard_sessions,
)

SCOPED_TABLES = [
    order_submissions,
    trades,
    wizard_sessions,
    wizard_combo_attempts,
    positions,
    account_snapshots,
    nav_history,
]


@pytest.mark.parametrize("table", SCOPED_TABLES, ids=lambda t: t.name)
def test_scope_columns_exist(table):
    col_names = {c.name for c in table.columns}
    assert "broker" in col_names, f"{table.name} missing broker"
    assert "account_env" in col_names, f"{table.name} missing account_env"
    assert "broker_account" in col_names, f"{table.name} missing broker_account"


def test_nav_history_pk_is_scoped():
    pk_cols = [c.name for c in nav_history.primary_key.columns]
    assert pk_cols == ["broker", "account_env", "broker_account", "date"]


def test_order_idempotency_constraint_is_scoped():
    uq = None
    for c in order_submissions.constraints:
        if getattr(c, "name", None) == "uq_order_sub_user_attempt":
            uq = c
            break
    assert uq is not None
    col_names = [col.name for col in uq.columns]
    assert "broker" in col_names
    assert "account_env" in col_names
    assert "broker_account" in col_names
    assert "user_id" in col_names
    assert "client_attempt_id" in col_names
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/db/tests/test_schema_scope.py -xvs
```

Expected: FAIL — `assert "broker" in col_names` for the first table.

- [ ] **Step 3: Add scope columns to schema.py**

Edit `src/xenon/db/schema.py`. For each of the 7 tables, add three columns. The exact changes:

**order_submissions** — add columns before the `UniqueConstraint`, update the constraint and indexes:

```python
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
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_order_sub_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_order_sub_account_env",
    ),
    UniqueConstraint(
        "broker", "account_env", "broker_account", "user_id", "client_attempt_id",
        name="uq_order_sub_user_attempt",
    ),
    Index("ix_order_sub_state_ticker", "state", "ticker"),
    Index("ix_order_sub_perm_id", "broker", "account_env", "broker_account", "perm_id"),
    Index("ix_order_sub_ib_order_id", "broker", "account_env", "broker_account", "ib_order_id"),
)
```

**trades** — add columns + execution-only CHECK:

```python
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
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_trades_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_trades_account_env",
    ),
)
```

**wizard_sessions** — add columns + execution-only CHECK:

```python
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
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_wizard_sess_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_wizard_sess_account_env",
    ),
)
```

**wizard_combo_attempts** — add columns + execution-only CHECK:

```python
wizard_combo_attempts = Table(
    "wizard_combo_attempts",
    xenon_metadata,
    Column("attempt_id", Text, primary_key=True),
    Column(
        "session_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"),
        nullable=False,
    ),
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
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_wizard_attempt_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_wizard_attempt_account_env",
    ),
    Index("ix_wizard_attempts_session_updated", "session_id", "updated_at"),
)
```

**positions** — keep existing `account` column, add new scope columns with portfolio CHECK:

```python
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
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_positions_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_positions_account_env",
    ),
)
```

**account_snapshots** — keep existing `account` column, add new scope columns with portfolio CHECK:

```python
account_snapshots = Table(
    "account_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("account", Text, nullable=False),
    Column("bankroll", Numeric(14, 2), nullable=False),
    Column("peak_value", Numeric(14, 2)),
    Column("net_liquidation", Numeric(14, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_acct_snap_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_acct_snap_account_env",
    ),
)
```

**nav_history** — change PK from `date` to composite, add scope columns with portfolio CHECK:

```python
nav_history = Table(
    "nav_history",
    xenon_metadata,
    Column("broker", Text, primary_key=True, server_default=text("'IB'")),
    Column("account_env", Text, primary_key=True, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, primary_key=True, server_default=text("'legacy_unknown'")),
    Column("date", Date, primary_key=True),
    Column("nav", Numeric(14, 2), nullable=False),
    Column("daily_pnl", Numeric(12, 2)),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_nav_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_nav_account_env",
    ),
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest src/xenon/db/tests/test_schema_scope.py -xvs
```

Expected: PASS (3 tests).

- [ ] **Step 5: Generate the Alembic migration**

The nav_history PK change cannot be fully autogenerated — Alembic will detect column additions but not PK restructuring cleanly. Generate first, then hand-edit.

```bash
uv run alembic revision --autogenerate -m "add broker account scope columns"
```

After generation, open the new migration file and verify it includes:

1. `op.add_column` for broker, account_env, broker_account on all 7 tables
2. `op.create_check_constraint` for all CHECK constraints
3. For order_submissions: drop old unique constraint, create new scoped one; drop + recreate scoped indexes
4. For nav_history: the PK restructure needs hand-editing:

```python
# nav_history PK restructure — must be hand-edited
op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
op.add_column("nav_history", sa.Column("broker", sa.Text(), server_default=sa.text("'IB'"), nullable=False), schema="xenon")
op.add_column("nav_history", sa.Column("account_env", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False), schema="xenon")
op.add_column("nav_history", sa.Column("broker_account", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False), schema="xenon")
op.create_primary_key("nav_history_pkey", "nav_history", ["broker", "account_env", "broker_account", "date"], schema="xenon")
```

- [ ] **Step 6: Apply the migration to the dev database**

```bash
uv run alembic upgrade head
```

Expected: migration applies cleanly.

- [ ] **Step 7: Verify migration state is clean**

```bash
uv run alembic check
```

Expected: no further changes detected.

- [ ] **Step 8: Run the full Python test suite to catch regressions**

```bash
uv run pytest -x
```

Expected: PASS. Existing tests still write the old columns; server_default fills the new ones. If any test fails due to the changed nav_history PK, fix the `upsert_nav` call sites to include scope columns (will be properly fixed in Task 6).

- [ ] **Step 9: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/ src/xenon/db/tests/test_schema_scope.py
git commit -m "feat(db): add broker/account_env/broker_account scope columns to 7 tables"
```

---

## Task 2: AccountScope Resolver

**Files:**

- Create: `src/xenon/execution/account_scope.py`
- Create: `src/xenon/db/tests/test_account_scope.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/db/tests/test_account_scope.py`:

```python
"""Unit tests for AccountScope resolver."""
from __future__ import annotations

import pytest

from xenon.execution.account_scope import AccountScope, resolve_from_env


def test_scope_is_frozen():
    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
    with pytest.raises(AttributeError):
        scope.broker = "FUTU"


def test_scope_dict():
    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
    d = scope.as_dict()
    assert d == {"broker": "IB", "account_env": "paper", "broker_account": "DU1234567"}


def test_resolve_from_env_paper(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU9999999")
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "paper"
    assert scope.broker_account == "DU9999999"


def test_resolve_from_env_live(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1234567")
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "live"
    assert scope.broker_account == "U1234567"


def test_resolve_from_env_missing_account_raises(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    with pytest.raises(ValueError, match="XENON_BROKER_ACCOUNT"):
        resolve_from_env()


def test_resolve_from_env_mismatch_raises(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    with pytest.raises(ValueError, match="mismatch"):
        resolve_from_env()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/db/tests/test_account_scope.py -xvs
```

Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/xenon/execution/account_scope.py`:

```python
"""Broker account scope — durable identity for every execution/portfolio row.

Two resolution paths:
1. FastAPI: `resolve_from_app_state(request)` reads `app.state.{trading_mode, account}`.
2. Sync callers (ib_sync, ib_execute, rehydrate): `resolve_from_env()` reads
   `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT` env vars. The IB sync path
   should set `XENON_BROKER_ACCOUNT` from `managedAccounts()[0]` at connect time.

Both paths return a frozen AccountScope that gets stamped on every DB write.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AccountScope:
    broker: Literal["IB", "FUTU"]
    account_env: Literal["paper", "live", "sim", "legacy_unknown"]
    broker_account: str

    def as_dict(self) -> dict[str, str]:
        return {
            "broker": self.broker,
            "account_env": self.account_env,
            "broker_account": self.broker_account,
        }


_MODE_TO_PREFIX: dict[str, str] = {"paper": "DU", "live": "U"}


def resolve_from_env() -> AccountScope:
    """Build scope from environment variables. Used by sync callers."""
    from xenon.api.trading_mode import MODE

    account = os.environ.get("XENON_BROKER_ACCOUNT", "").strip()
    if not account:
        raise ValueError(
            "XENON_BROKER_ACCOUNT must be set (e.g. DU1234567). "
            "The IB sync path should set this from managedAccounts()[0]."
        )
    expected_prefix = _MODE_TO_PREFIX.get(MODE, "")
    if expected_prefix and not account.startswith(expected_prefix):
        raise ValueError(
            f"XENON_BROKER_ACCOUNT={account!r} does not match "
            f"XENON_TRADING_MODE={MODE!r} (expected prefix {expected_prefix!r}) — mismatch"
        )
    return AccountScope(broker="IB", account_env=MODE, broker_account=account)


def resolve_from_app_state(app_state) -> AccountScope:
    """Build scope from FastAPI app.state. Used by async route handlers."""
    mode = getattr(app_state, "trading_mode", None)
    account = getattr(app_state, "account", None)
    if not mode or not account:
        raise ValueError(
            "app.state.trading_mode and app.state.account must be set "
            "(populated by server lifespan guard)"
        )
    return AccountScope(broker="IB", account_env=mode, broker_account=account)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest src/xenon/db/tests/test_account_scope.py -xvs
```

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/account_scope.py src/xenon/db/tests/test_account_scope.py
git commit -m "feat(execution): add AccountScope resolver for broker/env/account identity"
```

---

## Task 3: Scope Order Idempotency — orders_store.py (Sync Path)

**Files:**

- Modify: `src/xenon/execution/orders_store.py`
- Modify: `src/xenon/execution/ib_execute.py:317-339`
- Modify: `src/xenon/db/queries/trades.py`
- Test: extend existing order tests or add new ones

- [ ] **Step 1: Write the failing test**

Add to `src/xenon/db/tests/test_schema_scope.py`:

```python
def test_paper_live_orders_do_not_collide():
    """Same user + client_attempt_id in paper and live must create 2 rows."""
    from xenon.execution.orders_store import RequestRow, reserve_attempt

    req = RequestRow(
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        multiplier=1,
        limit_price=Decimal("150.00"),
    )
    # Paper reservation
    r1 = reserve_attempt(
        "local", "cid-001", req,
        broker="IB", account_env="paper", broker_account="DU1111111",
    )
    assert r1.status == "winner"

    # Live reservation with same user + cid
    r2 = reserve_attempt(
        "local", "cid-001", req,
        broker="IB", account_env="live", broker_account="U2222222",
    )
    assert r2.status == "winner"
    assert r1.submission_id != r2.submission_id
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/db/tests/test_schema_scope.py::test_paper_live_orders_do_not_collide -xvs
```

Expected: FAIL — `reserve_attempt() got an unexpected keyword argument 'broker'`.

- [ ] **Step 3: Update reserve_attempt to accept and persist scope**

In `src/xenon/execution/orders_store.py`, modify `reserve_attempt`:

```python
def reserve_attempt(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    db_path: Path | str | None = None,
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> ReservationOutcome:
    """Atomically reserve a submission slot keyed by (broker, account_env, broker_account, user_id, client_attempt_id)."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        stmt = pg_insert(order_submissions).values(
            submission_id=sid,
            user_id=user_id,
            client_attempt_id=client_attempt_id,
            ticker=request.ticker,
            security_type=request.security_type,
            action=request.action,
            quantity=request.quantity,
            expiry=request.expiry,
            strike=request.strike,
            right=request.right,
            multiplier=request.multiplier,
            con_id=request.con_id,
            limit_price=request.limit_price,
            state="PENDING",
            submitted_at=now,
            updated_at=now,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
        stmt = stmt.returning(order_submissions.c.submission_id)
        inserted = conn.execute(stmt).first()

        if inserted is not None:
            return ReservationOutcome(
                status="winner",
                submission_id=sid,
                state="PENDING",
                duplicate_of=None,
                reason_code=None,
            )

        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.state,
                order_submissions.c.ib_order_id,
                order_submissions.c.reason_code,
            ).where(
                order_submissions.c.broker == broker,
                order_submissions.c.account_env == account_env,
                order_submissions.c.broker_account == broker_account,
                order_submissions.c.user_id == user_id,
                order_submissions.c.client_attempt_id == client_attempt_id,
            )
        ).first()
        assert row is not None, "ON CONFLICT hit but row not found"
        existing_sid, state, ib_order_id, reason_code = row
        if state in _TERMINAL_STATES:
            return ReservationOutcome(
                status="terminal",
                submission_id=existing_sid,
                state=state,
                duplicate_of=None,
                reason_code=reason_code,
            )
        return ReservationOutcome(
            status="duplicate",
            submission_id=existing_sid,
            state=state,
            duplicate_of=ib_order_id,
            reason_code=None,
        )
```

Apply the same pattern to `register_from_snapshot` — add `broker`, `account_env`, `broker_account` keyword args, pass them into the INSERT values dict.

- [ ] **Step 4: Update lookup_by_attempt to accept scope**

```python
def lookup_by_attempt(
    user_id: str,
    client_attempt_id: str,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> SubmissionRow | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        conditions = [
            order_submissions.c.user_id == user_id,
            order_submissions.c.client_attempt_id == client_attempt_id,
        ]
        if broker is not None:
            conditions.append(order_submissions.c.broker == broker)
        if account_env is not None:
            conditions.append(order_submissions.c.account_env == account_env)
        if broker_account is not None:
            conditions.append(order_submissions.c.broker_account == broker_account)
        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.user_id,
                order_submissions.c.ticker,
                order_submissions.c.state,
                order_submissions.c.ib_order_id,
                order_submissions.c.perm_id,
                order_submissions.c.placing_client_id,
                order_submissions.c.reason_code,
                order_submissions.c.quantity,
                order_submissions.c.action,
                order_submissions.c.security_type,
                order_submissions.c.right,
                order_submissions.c.expiry,
            ).where(*conditions)
        ).first()
    if row is None:
        return None
    vals = list(row)
    if vals[12] is not None and not isinstance(vals[12], str):
        vals[12] = str(vals[12])
    return SubmissionRow(*vals)
```

- [ ] **Step 5: Update ib_execute.py trade insert to accept scope**

In `src/xenon/execution/ib_execute.py` around line 325, add scope columns to the trade insert:

```python
conn.execute(
    insert(trades).values(
        ticker=result["symbol"],
        structure=structure,
        action=side,
        quantity=result["quantity"],
        entry_cost=Decimal(str(round(result["total_value"], 4))) if side == "BUY" else None,
        exit_cost=Decimal(str(round(result["total_value"], 4))) if side == "SELL" else None,
        edge=thesis or None,
        decision="EXECUTED",
        opened_at=now_utc if side == "BUY" else None,
        closed_at=now_utc if side == "SELL" else None,
        metadata=trade_entry,
        broker=os.environ.get("XENON_BROKER", "IB"),
        account_env=os.environ.get("XENON_TRADING_MODE", "legacy_unknown"),
        broker_account=os.environ.get("XENON_BROKER_ACCOUNT", "legacy_unknown"),
    )
)
```

(ib_execute.py runs as a subprocess, so it reads from env, not app.state.)

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest src/xenon/db/tests/test_schema_scope.py -xvs
uv run pytest scripts/tests/ -x -k "order" --no-header -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/execution/orders_store.py src/xenon/execution/ib_execute.py src/xenon/db/tests/test_schema_scope.py
git commit -m "fix(db): scope order idempotency and trade writes by broker account"
```

---

## Task 4: Scope Async Order Queries (FastAPI Path)

**Files:**

- Modify: `src/xenon/db/queries/orders.py`
- Modify: `src/xenon/db/queries/trades.py`
- Modify: `src/xenon/api/server.py` (order route call sites)
- Modify: `src/xenon/api/guards.py`

- [ ] **Step 1: Add get_account_scope dependency to guards.py**

In `src/xenon/api/guards.py`, add:

```python
from xenon.execution.account_scope import AccountScope, resolve_from_app_state


def get_account_scope(request: Request) -> AccountScope:
    """FastAPI dependency: resolve the current broker account scope from app.state."""
    return resolve_from_app_state(request.app.state)
```

- [ ] **Step 2: Update async reserve_attempt in orders.py**

In `src/xenon/db/queries/orders.py`, add scope params to `reserve_attempt`:

```python
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
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
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
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    stmt = pg_insert(order_submissions).values(**values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
    stmt = stmt.returning(order_submissions.c.submission_id)
    result = await conn.execute(stmt)
    inserted = result.first()
    if inserted is not None:
        return await get_by_submission_id(conn, submission_id)
    existing = await lookup_by_attempt(conn, user_id, client_attempt_id)
    return existing
```

- [ ] **Step 3: Update working_orders_for to filter by scope**

```python
async def working_orders_for(
    conn: AsyncConnection,
    *,
    user_id: str,
    ticker: str,
    broker: str = "IB",
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    conditions = [
        order_submissions.c.user_id == user_id,
        order_submissions.c.ticker == ticker,
        order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED"]),
        order_submissions.c.broker == broker,
    ]
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    stmt = select(order_submissions).where(*conditions)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
```

- [ ] **Step 4: Update trades.py append_trade**

In `src/xenon/db/queries/trades.py`, add scope params to the trade append function. (Read the file first to see the exact signature.)

- [ ] **Step 5: Wire scope through server.py order routes**

In the `/orders/place` route in `src/xenon/api/server.py`, inject `AccountScope` via the `get_account_scope` dependency and pass `scope.as_dict()` to the order reservation and trade write call sites. The exact changes depend on how the route currently calls the query functions — thread `scope.broker`, `scope.account_env`, `scope.broker_account` through as keyword args.

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest -x
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/db/queries/orders.py src/xenon/db/queries/trades.py src/xenon/api/guards.py src/xenon/api/server.py
git commit -m "fix(orders): persist and filter broker account scope in async order paths"
```

---

## Task 5: Scope Wizard Lifecycle

**Files:**

- Modify: `src/xenon/db/queries/combo_wizard.py`
- Modify: `src/xenon/db/queries/wizard.py`
- Modify: `src/xenon/api/routes/wizard.py` (if exists; otherwise the wizard routes in `server.py`)

- [ ] **Step 1: Add scope to create_session in combo_wizard.py**

In `src/xenon/db/queries/combo_wizard.py`, update `create_session`:

```python
def create_session(
    conn: Connection,
    *,
    session_id: str,
    ticker: str,
    state: str,
    structure_name: str | None = None,
    intent: str | None = None,
    payload: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    now = created_at or datetime.now(timezone.utc)
    conn.execute(
        insert(wizard_sessions).values(
            session_id=session_id,
            ticker=ticker,
            state=state,
            structure_name=structure_name,
            intent=intent,
            payload=payload,
            created_at=now,
            updated_at=updated_at or now,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    )
```

- [ ] **Step 2: Add scope to create_attempt**

```python
def create_attempt(conn: Connection, **fields) -> None:
    if "broker" not in fields:
        fields["broker"] = "IB"
    if "account_env" not in fields:
        fields["account_env"] = "legacy_unknown"
    if "broker_account" not in fields:
        fields["broker_account"] = "legacy_unknown"
    conn.execute(insert(wizard_combo_attempts).values(**fields))
```

- [ ] **Step 3: Filter list_rehydratable and list_sessions by scope**

```python
def list_rehydratable(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    conditions = [
        wizard_sessions.c.state.in_(
            ["submitting", "working", "reprice_pending", "protection_pending", "protected"]
        )
    ]
    if broker is not None:
        conditions.append(wizard_sessions.c.broker == broker)
    if account_env is not None:
        conditions.append(wizard_sessions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(wizard_sessions.c.broker_account == broker_account)
    result = conn.execute(select(wizard_sessions).where(*conditions))
    return [dict(r._mapping) for r in result]
```

Apply the same scope-filter pattern to `list_sessions` and `list_protected_sessions` (the raw SQL in `list_protected_sessions` needs a WHERE clause extension for the scope columns).

- [ ] **Step 4: Update async wizard.py with the same scope params**

In `src/xenon/db/queries/wizard.py`, update `create_session` to accept `broker`, `account_env`, `broker_account` keyword args and pass them through to the INSERT.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest -x
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/queries/combo_wizard.py src/xenon/db/queries/wizard.py
git commit -m "fix(wizard): isolate sessions and attempts by broker account scope"
```

---

## Task 6: Scope Portfolio, NAV, Account Snapshots

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:1094-1117, 1128-1135`
- Modify: `src/xenon/db/queries/portfolio.py`

- [ ] **Step 1: Update save_positions in portfolio.py**

```python
async def save_positions(
    conn: AsyncConnection,
    rows: list[dict],
    *,
    account: str,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    await conn.execute(
        delete(positions).where(
            positions.c.account == account,
            positions.c.broker == broker,
            positions.c.account_env == account_env,
            positions.c.broker_account == broker_account,
        )
    )
    for row in rows:
        row.setdefault("broker", broker)
        row.setdefault("account_env", account_env)
        row.setdefault("broker_account", broker_account)
        await conn.execute(insert(positions).values(**row))
```

- [ ] **Step 2: Update save_account_snapshot**

```python
async def save_account_snapshot(
    conn: AsyncConnection,
    *,
    account: str,
    bankroll: Decimal,
    peak_value: Decimal | None = None,
    net_liquidation: Decimal | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    await conn.execute(
        insert(account_snapshots).values(
            account=account,
            bankroll=bankroll,
            peak_value=peak_value,
            net_liquidation=net_liquidation,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    )
```

- [ ] **Step 3: Update upsert_nav for scoped PK**

```python
async def upsert_nav(
    conn: AsyncConnection,
    day: date,
    *,
    nav: Decimal,
    daily_pnl: Decimal | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    stmt = pg_insert(nav_history).values(
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
        date=day,
        nav=nav,
        daily_pnl=daily_pnl,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            nav_history.c.broker,
            nav_history.c.account_env,
            nav_history.c.broker_account,
            nav_history.c.date,
        ],
        set_={"nav": stmt.excluded.nav, "daily_pnl": stmt.excluded.daily_pnl},
    )
    await conn.execute(stmt)
```

- [ ] **Step 4: Update ib_sync.py to use real scope**

In `src/xenon/execution/ib_sync.py`, the `_save_portfolio_to_postgres` function currently hardcodes `account="IB"`. Replace with actual scope. Since ib_sync runs as a sync subprocess, it should resolve scope from env:

At the top of `_save_portfolio_to_postgres`, add:

```python
_broker = os.environ.get("XENON_BROKER", "IB")
_account_env = os.environ.get("XENON_TRADING_MODE", "legacy_unknown")
_broker_account = os.environ.get("XENON_BROKER_ACCOUNT", "legacy_unknown")
```

Then replace all `account="IB"` with `account=_broker_account` and add the three scope columns to every INSERT:

```python
conn.execute(
    insert(positions).values(
        ticker=ticker,
        security_type=sec_type,
        expiry=expiry,
        strike=Decimal(str(leg["strike"])) if leg.get("strike") else None,
        right=right,
        quantity=qty,
        avg_cost=Decimal(str(leg.get("avg_cost", 0))),
        current_price=Decimal(str(leg["market_price"])) if leg.get("market_price") else None,
        unrealized_pnl=None,
        account=_broker_account,
        broker=_broker,
        account_env=_account_env,
        broker_account=_broker_account,
    )
)
```

And for the positions DELETE:

```python
conn.execute(
    delete(positions).where(
        positions.c.broker == _broker,
        positions.c.account_env == _account_env,
        positions.c.broker_account == _broker_account,
    )
)
```

Same for `account_snapshots` insert and the `_append_nav_snapshot` function.

- [ ] **Step 5: Run tests**

```bash
uv run pytest -x
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/queries/portfolio.py src/xenon/execution/ib_sync.py
git commit -m "fix(portfolio): scope positions, snapshots, and NAV by broker account"
```

---

## Task 7: Scope Rehydrate and Monitor Filtering

**Files:**

- Modify: `src/xenon/execution/single_leg_rehydrate.py:200-208`
- Modify: `src/xenon/execution/combo_wizard/rehydrate.py:57-65`
- Modify: `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py:87-113`
- Modify: `src/xenon/db/queries/combo_wizard.py` (list_unresolved_orders, list_protected_sessions)

- [ ] **Step 1: Filter list_unresolved_orders by scope**

In `src/xenon/db/queries/combo_wizard.py`, update `list_unresolved_orders`:

```python
def list_unresolved_orders(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    from xenon.db.schema import order_submissions

    conditions = [order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED"])]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    result = conn.execute(select(order_submissions).where(*conditions))
    return [dict(r._mapping) for r in result]
```

- [ ] **Step 2: Filter list_protected_sessions by scope**

Update the raw SQL in `list_protected_sessions`:

```python
def list_protected_sessions(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    base_sql = """
        SELECT s.session_id, s.ticker, s.payload, p.config, p.state as protection_state
        FROM xenon.wizard_sessions s
        JOIN xenon.wizard_protection p ON p.session_id = s.session_id
        WHERE UPPER(s.state) = 'PROTECTED'
          AND p.state = 'active'
    """
    params: dict = {}
    if broker is not None:
        base_sql += " AND s.broker = :broker"
        params["broker"] = broker
    if account_env is not None:
        base_sql += " AND s.account_env = :account_env"
        params["account_env"] = account_env
    if broker_account is not None:
        base_sql += " AND s.broker_account = :broker_account"
        params["broker_account"] = broker_account
    result = conn.execute(text(base_sql), params)
    return [dict(r._mapping) for r in result]
```

- [ ] **Step 3: Thread scope through single_leg_rehydrate**

In `src/xenon/execution/single_leg_rehydrate.py`, update `_list_unresolved` to accept and pass scope:

```python
def _list_unresolved(
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = combo_wizard.list_unresolved_orders(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    for r in rows:
        if isinstance(r.get("expiry"), date):
            r["expiry"] = r["expiry"].isoformat()
    return rows
```

Update `rehydrate_on_boot` to accept scope params and pass them to `_list_unresolved`:

```python
def rehydrate_on_boot(
    ib_client_factory: Callable[[], Any],
    orders_store,
    now: Callable[[], float] = time.time,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[ReconcileDecision]:
    rows = _list_unresolved(
        db_path=db_path,
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    # ... rest unchanged
```

- [ ] **Step 4: Thread scope through combo wizard rehydrate**

In `src/xenon/execution/combo_wizard/rehydrate.py`, update `_list_rehydratable` and `rehydrate_combo_sessions`:

```python
def _list_rehydratable(
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = combo_wizard.list_rehydratable(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    for r in rows:
        if r.get("payload") is None:
            r["payload"] = {}
    return rows
```

Update `rehydrate_combo_sessions` to accept and pass scope kwargs.

- [ ] **Step 5: Thread scope through wizard_stop_monitor**

In `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`, update `_list_protected` to pass scope. The handler should read scope from env since it runs as a daemon:

```python
def _list_protected(self) -> list[dict[str, Any]]:
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard

    broker = os.environ.get("XENON_BROKER", "IB")
    account_env = os.environ.get("XENON_TRADING_MODE")
    broker_account = os.environ.get("XENON_BROKER_ACCOUNT")

    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = combo_wizard.list_protected_sessions(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    # ... rest unchanged
```

- [ ] **Step 6: Update call sites in server.py lifespan**

The FastAPI lifespan calls `rehydrate_on_boot` and `rehydrate_combo_sessions`. Pass scope from `app.state`:

```python
# In lifespan, after mode verification:
_scope_kwargs = {
    "broker": "IB",
    "account_env": app.state.trading_mode,
    "broker_account": app.state.account,
}

decisions = rehydrate_on_boot(
    ib_client_factory=...,
    orders_store=...,
    **_scope_kwargs,
)
combo_decisions = rehydrate_combo_sessions(
    ib_client_factory=...,
    **_scope_kwargs,
)
```

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest -x
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/xenon/execution/single_leg_rehydrate.py src/xenon/execution/combo_wizard/rehydrate.py src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py src/xenon/db/queries/combo_wizard.py src/xenon/api/server.py
git commit -m "fix(execution): filter rehydrate and monitor flows by broker account scope"
```

---

## Task 8: Legacy Data Policy Documentation

**Files:**

- Update: `docs/architecture/production-database-strategy.md` (or create if not checked in)
- Update: `CLAUDE.md`
- Update: `src/xenon/CLAUDE.md`

- [ ] **Step 1: Document the scope policy**

Add a section to `docs/architecture/production-database-strategy.md`:

```markdown
## Broker Account Scope

Every execution and portfolio row carries three scope columns:

| Column           | Values                                   | Meaning                           |
| ---------------- | ---------------------------------------- | --------------------------------- |
| `broker`         | `IB`, `FUTU`                             | Originating broker                |
| `account_env`    | `paper`, `live`, `sim`, `legacy_unknown` | Runtime environment at write time |
| `broker_account` | e.g. `DU1234567`, `U9876543`             | External account ID               |

**Rules:**

- Execution tables (order*submissions, trades, wizard*\*) enforce `broker = 'IB'`.
- Portfolio tables (positions, account_snapshots, nav_history) allow `IB` and `FUTU`.
- `legacy_unknown` rows are pre-scope historical data. They are excluded from active execution workflows (rehydrate, monitor, working-orders queries filter by the current scope).
- Futu is read-only — no Futu rows in order/trade/wizard tables until a future migration explicitly enables it.
- Operators may manually classify legacy rows later; automated backfill is not planned.

**Environment variables for sync callers:**

- `XENON_TRADING_MODE` — `paper` or `live` (drives `account_env`)
- `XENON_BROKER_ACCOUNT` — the actual IB account ID (set by IB sync from `managedAccounts()[0]`)

**FastAPI callers:** Resolve scope from `app.state.trading_mode` + `app.state.account` via `AccountScope.resolve_from_app_state()`.
```

- [ ] **Step 2: Update CLAUDE.md Database section**

Add to the Database section of `src/xenon/CLAUDE.md`:

```markdown
### Broker Account Scope

All execution/portfolio tables carry `broker`, `account_env`, `broker_account` columns. See `docs/architecture/production-database-strategy.md` for the full policy. Key rules:

- Every write must include scope — never rely on server_default for new rows.
- Every query in an active workflow (rehydrate, monitor, working-orders) must filter by scope.
- `legacy_unknown` rows are excluded from active flows.
- Use `AccountScope` from `src/xenon/execution/account_scope.py` — never hardcode scope values in query code.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/production-database-strategy.md CLAUDE.md src/xenon/CLAUDE.md
git commit -m "docs(db): document broker account scope policy and legacy data rules"
```

---

## Task 9: Final Verification

- [ ] **Step 1: Run Alembic check**

```bash
uv run alembic upgrade head
uv run alembic check
```

Expected: migration is applied and no further changes detected.

- [ ] **Step 2: Run full Python test suite**

```bash
uv run pytest -x
```

Expected: PASS.

- [ ] **Step 3: Run API tests specifically**

```bash
uv run pytest src/xenon/api/tests/ -x -q
```

Expected: PASS.

- [ ] **Step 4: Run web tests**

```bash
cd web && npm test
```

Expected: PASS (web code doesn't directly touch DB scope, but API contract changes may surface).

- [ ] **Step 5: Verify scope columns exist in running DB**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT column_name FROM information_schema.columns WHERE table_schema='xenon' AND table_name='order_submissions' AND column_name IN ('broker','account_env','broker_account');"
```

Expected: 3 rows.

- [ ] **Step 6: Verify nav_history PK is composite**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'xenon.nav_history'::regclass AND i.indisprimary ORDER BY a.attnum;"
```

Expected: broker, account_env, broker_account, date.

- [ ] **Step 7: Verify paper/live isolation with a manual SQL test**

```sql
-- Should succeed (paper)
INSERT INTO xenon.order_submissions (submission_id, user_id, client_attempt_id, ticker, security_type, action, quantity, limit_price, state, submitted_at, updated_at, broker, account_env, broker_account)
VALUES ('test-paper', 'local', 'cid-test', 'AAPL', 'STK', 'BUY', 100, 150.00, 'PENDING', now(), now(), 'IB', 'paper', 'DU0000000');

-- Should succeed (live, same user+cid, different scope)
INSERT INTO xenon.order_submissions (submission_id, user_id, client_attempt_id, ticker, security_type, action, quantity, limit_price, state, submitted_at, updated_at, broker, account_env, broker_account)
VALUES ('test-live', 'local', 'cid-test', 'AAPL', 'STK', 'BUY', 100, 150.00, 'PENDING', now(), now(), 'IB', 'live', 'U0000000');

-- Clean up
DELETE FROM xenon.order_submissions WHERE submission_id IN ('test-paper', 'test-live');
```

- [ ] **Step 8: Final commit if any cleanup needed**

Only if verification revealed issues. Otherwise, done.

---

## Risk Notes

1. **nav_history PK migration** is the riskiest step — dropping and recreating the PK. If the table has data, the migration is still safe because all existing rows share the same default values (`IB`, `legacy_unknown`, `legacy_unknown`) so the composite PK remains unique. But run `uv run alembic upgrade head` on a test DB first.

2. **positions.account column overlap**: The existing `account` column is kept for backward compatibility. Code that reads `account` still works. A follow-up migration should drop `account` once all callers use `broker_account` instead.

3. **Futu Postgres writes**: `futu_sync.py` currently writes to JSON, not Postgres. When Futu DB writes are added, they should use `broker='FUTU'` + `account_env='live'` + the actual Futu account ID.

4. **Test fixtures**: Many existing tests don't pass scope. The `server_default` values ensure they still work, but new tests for scoped behavior must explicitly pass `broker`/`account_env`/`broker_account`.

---

## Plan Self-Review

- **Spec coverage:** Every table identified in the analysis has a task. All write paths (order reserve, trade log, portfolio sync, wizard create, rehydrate queries, monitor queries) are covered. The AccountScope resolver handles both FastAPI and sync paths.
- **Type consistency:** `AccountScope` dataclass used consistently; `as_dict()` method returns the three columns; `resolve_from_app_state` and `resolve_from_env` are the two entry points — names are stable across all tasks.
- **Placeholder scan:** No TBD/TODO — every step shows the actual code. The `ib_execute.py` step uses `os.environ` directly because it runs as a subprocess (not app.state); this is intentional, not a placeholder.
- **Backward compatibility:** All new params have defaults matching legacy behavior (`broker="IB"`, `account_env="legacy_unknown"`, `broker_account="legacy_unknown"`), so existing callers and tests work without changes.
- **Soft spot:** Task 4 (async order queries) and Task 6 (portfolio) require reading the exact FastAPI route call sites to thread scope through. The plan shows the query-layer changes in full but describes the route-layer wiring at a high level because route code is large and the pattern is mechanical (inject `AccountScope` dependency, pass `.as_dict()` kwargs).
