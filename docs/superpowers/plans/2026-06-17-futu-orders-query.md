# Futu Order Querying — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface Futu account orders (open / executed / historical / journal) in the terminal exactly like IB — read-only, DB-first, unified onto the IB `OpenOrder`/`ExecutedOrder` data structure and the existing REST routes.

**Architecture:** A sync writer pulls orders/fills/fees from Futu OpenD and persists to three new Postgres tables (`futu_orders`, `futu_order_fees`, `futu_closed_trades`); a shared FIFO lot-matcher feeds both the 30-day historical table and `FUTU_AUTO_IMPORT` journal rows. The read path adds a `broker=="FUTU"` branch to the existing `/orders`, `/blotter`, `/journal` routes that shapes Futu rows into the identical IB response contract. The frontend threads the existing `activeAccount` state into the data hooks via a `?broker=` query param. The HTTP request path never touches OpenD.

**Tech Stack:** Python 3.13 / `uv`, FastAPI, SQLAlchemy Core + asyncpg, Alembic, `futu-api` SDK, Next.js 16 / React, Vitest, chrome-cdp E2E.

## Global Constraints

- **DB-first, read-only:** Futu data is written to Postgres by the sync writer; the API only reads. No OpenD calls on the request path. No order placement/modify/cancel for Futu.
- **`uv` for all Python:** `uv run pytest …`, `uv run alembic …`. Never bare `python`/`pip`.
- **Broker Account Scope on every row/query:** all writes carry `(broker='FUTU', account_env, broker_account)`; all reads filter by `AccountScope`. Never rely on `server_default`.
- **`XENON_READ_ONLY=1` honored:** every new persistence path no-ops under read-only mode (mirror `ib_sync._save_portfolio_to_postgres`).
- **Migrations on dev DB only:** `uv run alembic upgrade head` against the dev DB; the macmini Docker `migrator` applies to `core_dev` on deploy. Never point migrations at `core_dev`.
- **Order types are display-faithful:** map `NORMAL→LMT`, `MARKET→MKT`; pass through all other Futu order types as their label. TIF: `DAY`/`GTC`. No placement of any type.
- **TDD, frequent commits, no `Co-Authored-By` trailer.**
- **Test DB:** Postgres-backed tests use the autouse `_postgres_orders_test_db` fixture (BEGIN/ROLLBACK). Use `@pytest.mark.committed_db` only when a test forks a subprocess CLI or builds its own engine.

---

## File Structure

| File                                                                        | Responsibility                                                                                        | Action |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------ |
| `src/xenon/db/schema.py`                                                    | `futu_orders`, `futu_order_fees`, `futu_closed_trades` table defs + Futu journal partial-unique index | Modify |
| `src/xenon/db/migrations/versions/2026_06_17_futu_orders.py`                | Alembic migration for the three tables + index                                                        | Create |
| `src/xenon/db/queries/futu_history.py`                                      | `insert_orders`/`list_orders`, `insert_order_fees`, `insert_closed_trades`/`list_closed_trades`       | Modify |
| `src/xenon/db/queries/journal.py`                                           | `upsert_futu_auto_import_entry`                                                                       | Modify |
| `src/xenon/clients/futu_client.py`                                          | `fetch_open_orders`, `fetch_history_orders`, `fetch_order_fees`                                       | Modify |
| `src/xenon/api/services/futu_closed_trades.py`                              | Shared FIFO lot-matcher → closed-trade records                                                        | Create |
| `src/xenon/api/services/futu_nav_backfill.py`                               | `_compute_daily_realized_pnl` re-expressed over the shared matcher                                    | Modify |
| `src/xenon/api/services/futu_history_sync.py`                               | Persist orders/fees/closed-trades + journal upsert in the sync                                        | Modify |
| `src/xenon/api/server.py`                                                   | Market-hours open-orders poll loop; `/futu/sync` + `/orders/refresh?broker=FUTU` pull orders          | Modify |
| `src/xenon/api/guards.py`                                                   | Rename `get_performance_scope` → `get_broker_scope` (broker-aware dep)                                | Modify |
| `src/xenon/api/routes/orders.py`                                            | `broker=="FUTU"` branch + Futu shapers; broker-aware dep                                              | Modify |
| `src/xenon/api/routes/performance.py` (caller)                              | Update import to `get_broker_scope`                                                                   | Modify |
| `src/xenon/api/routes/blotter.py`                                           | `broker=="FUTU"` branch reading `futu_closed_trades`                                                  | Modify |
| `web/app/api/{orders,blotter,journal}/route.ts`                             | Forward `?broker=` to `xenonFetch`                                                                    | Modify |
| `web/lib/{useOrders,useBlotter,useJournal}.ts`                              | Accept `broker`, bake into fetch URL                                                                  | Modify |
| `web/components/WorkspaceShell.tsx`, `web/components/WorkspaceSections.tsx` | Pass `activeAccount`→`broker` into the hooks                                                          | Modify |

---

## Task 1: New Postgres tables + migration

**Files:**

- Modify: `src/xenon/db/schema.py` (after `futu_cash_flow`, ~line 294)
- Create: `src/xenon/db/migrations/versions/2026_06_17_futu_orders.py`
- Test: `scripts/tests/test_futu_orders_schema.py`

**Interfaces:**

- Produces: SQLAlchemy `Table` objects `futu_orders`, `futu_order_fees`, `futu_closed_trades` importable from `xenon.db.schema`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_futu_orders_schema.py
import pytest
from sqlalchemy import inspect, text
from xenon.db.engine import get_sync_engine

pytestmark = pytest.mark.committed_db  # asserts real DDL is applied


@pytest.mark.parametrize("table", ["futu_orders", "futu_order_fees", "futu_closed_trades"])
def test_futu_order_tables_exist(table):
    engine = get_sync_engine()
    insp = inspect(engine)
    assert insp.has_table(table, schema="xenon"), f"{table} missing — run alembic upgrade head"


def test_futu_orders_columns():
    engine = get_sync_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("futu_orders", schema="xenon")}
    expected = {
        "broker", "account_env", "broker_account", "futu_order_id",
        "ticker", "futu_code", "market", "action", "order_type",
        "quantity", "limit_price", "aux_price", "status", "tif",
        "filled_qty", "avg_fill_price", "created_at", "updated_at", "raw", "ingested_at",
    }
    assert expected <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_futu_orders_schema.py -v`
Expected: FAIL — `futu_orders missing`.

- [ ] **Step 3: Add the three tables to `schema.py`**

Insert after the `futu_cash_flow` table definition. Mirror the `futu_trades` PK/scope/check pattern.

```python
futu_orders = Table(
    "futu_orders",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_order_id", Text, primary_key=True),
    Column("ticker", Text, nullable=False),
    Column("futu_code", Text, nullable=False),
    Column("market", Text, nullable=False),
    Column("action", Text, nullable=False),  # BUY | SELL (normalized)
    Column("order_type", Text, nullable=False),  # raw Futu OrderType label
    Column("quantity", Numeric(20, 8), nullable=False),
    Column("limit_price", Numeric(14, 4)),
    Column("aux_price", Numeric(14, 4)),  # stop/trigger price
    Column("status", Text, nullable=False),  # raw Futu OrderStatus label
    Column("tif", Text, nullable=False, server_default=text("'DAY'")),
    Column("filled_qty", Numeric(20, 8), nullable=False, server_default=text("0")),
    Column("avg_fill_price", Numeric(14, 4)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_orders_broker"),
    CheckConstraint("account_env IN ('paper', 'live', 'sim')", name="ck_futu_orders_account_env"),
    CheckConstraint("action IN ('BUY', 'SELL')", name="ck_futu_orders_action"),
    Index("ix_futu_orders_scope_updated_at", "broker", "account_env", "broker_account", "updated_at"),
    Index("ix_futu_orders_scope_status", "broker", "account_env", "broker_account", "status"),
)

futu_order_fees = Table(
    "futu_order_fees",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_order_id", Text, primary_key=True),
    Column("total_fee", Numeric(14, 4), nullable=False, server_default=text("0")),
    Column("currency", Text, nullable=False, server_default=text("'USD'")),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_order_fees_broker"),
)

futu_closed_trades = Table(
    "futu_closed_trades",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_close_id", Text, primary_key=True),  # synthetic stable id (see Task 5)
    Column("ticker", Text, nullable=False),
    Column("futu_code", Text, nullable=False),
    Column("structure", Text),  # 'Long Stock' | 'Short Call' | ... (best-effort)
    Column("action", Text, nullable=False),  # closing side: SELL (was long) | BUY (was short)
    Column("quantity", Numeric(20, 8), nullable=False),
    Column("entry_cost", Numeric(14, 4)),
    Column("exit_cost", Numeric(14, 4)),
    Column("realized_pnl", Numeric(14, 2), nullable=False),
    Column("cost_basis", Numeric(14, 4), nullable=False),
    Column("proceeds", Numeric(14, 4), nullable=False),
    Column("opened_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("metadata", JSONB),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_closed_trades_broker"),
    Index("ix_futu_closed_scope_closed_at", "broker", "account_env", "broker_account", "closed_at"),
)
```

- [ ] **Step 4: Generate + apply the migration**

```bash
cd /Users/chenxi/projects/xenon/.worktrees/futu-orders-query
uv run alembic revision --autogenerate -m "futu_orders_and_fees_and_closed_trades"
# Rename the generated file to 2026_06_17_futu_orders.py; verify down_revision = current head.
uv run alembic upgrade head
```

Inspect the autogenerated migration: it must `op.create_table` all three tables and the indexes. Hand-edit only if autogenerate misses a CheckConstraint.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_futu_orders_schema.py -v`
Expected: PASS (4 cases).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/2026_06_17_futu_orders.py scripts/tests/test_futu_orders_schema.py
git commit -m "feat(futu): add futu_orders, futu_order_fees, futu_closed_trades tables"
```

---

## Task 2: Query helpers for the new tables

**Files:**

- Modify: `src/xenon/db/queries/futu_history.py`
- Test: `scripts/tests/test_futu_orders_queries.py`

**Interfaces:**

- Consumes: `futu_orders`, `futu_order_fees`, `futu_closed_trades` from Task 1; `AccountScope`; `_scoped`, `_chunks` already in the module.
- Produces:
  - `async insert_orders(engine, scope, rows) -> int`
  - `async list_orders(engine, scope, *, statuses: set[str] | None = None) -> list[dict]`
  - `async insert_order_fees(engine, scope, rows) -> int`
  - `async insert_closed_trades(engine, scope, rows) -> int`
  - `async list_closed_trades(engine, scope, since=None, until=None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_futu_orders_queries.py
import pytest
from datetime import datetime, timezone
from xenon.db.engine import get_async_engine_for_test  # see conftest helpers
from xenon.db.queries.futu_history import (
    insert_orders, list_orders, insert_closed_trades, list_closed_trades,
)
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="281753263")


@pytest.mark.asyncio
async def test_insert_orders_idempotent_upsert(pg_async_engine):
    row = {
        "futu_order_id": "O1", "ticker": "QQQ", "futu_code": "US.QQQ", "market": "US",
        "action": "BUY", "order_type": "NORMAL", "quantity": 1, "limit_price": 630.96,
        "aux_price": None, "status": "SUBMITTED", "tif": "GTC", "filled_qty": 0,
        "avg_fill_price": None,
        "created_at": datetime(2026, 6, 17, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 17, tzinfo=timezone.utc),
        "raw": {"order_id": "O1"},
    }
    assert await insert_orders(pg_async_engine, SCOPE, [row]) == 1
    # Re-pull with a status change → UPSERT, not duplicate.
    row2 = {**row, "status": "FILLED_ALL", "filled_qty": 1}
    await insert_orders(pg_async_engine, SCOPE, [row2])
    rows = await list_orders(pg_async_engine, SCOPE)
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED_ALL"


@pytest.mark.asyncio
async def test_list_orders_status_filter_and_scope_isolation(pg_async_engine):
    base = {
        "ticker": "QQQ", "futu_code": "US.QQQ", "market": "US", "action": "BUY",
        "order_type": "NORMAL", "quantity": 1, "limit_price": 1.0, "aux_price": None,
        "tif": "DAY", "filled_qty": 0, "avg_fill_price": None,
        "created_at": datetime(2026, 6, 17, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 17, tzinfo=timezone.utc), "raw": {},
    }
    await insert_orders(pg_async_engine, SCOPE, [
        {**base, "futu_order_id": "A", "status": "SUBMITTED"},
        {**base, "futu_order_id": "B", "status": "FILLED_ALL"},
    ])
    open_only = await list_orders(pg_async_engine, SCOPE, statuses={"SUBMITTED"})
    assert {r["futu_order_id"] for r in open_only} == {"A"}
    other = AccountScope(broker="FUTU", account_env="paper", broker_account="99")
    assert await list_orders(pg_async_engine, other) == []
```

> Use whatever async-engine fixture the existing `scripts/tests/test_futu_history_queries.py` uses (likely `pg_async_engine`). Match that fixture name exactly.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_futu_orders_queries.py -v`
Expected: FAIL — `ImportError: cannot import name 'insert_orders'`.

- [ ] **Step 3: Add the helpers to `futu_history.py`**

Import the new tables at the top (`futu_orders, futu_order_fees, futu_closed_trades`). Mirror `insert_trades`/`list_trades` exactly. `insert_orders` UPSERTs every mutable column:

```python
async def insert_orders(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    # 20 cols → keep batches < 32767/20 ≈ 1600 rows.
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, 1500):
            stmt = pg_insert(futu_orders).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_order_id"],
                set_={
                    c.name: getattr(stmt.excluded, c.name)
                    for c in futu_orders.columns
                    if c.name not in {"broker", "account_env", "broker_account", "futu_order_id", "ingested_at"}
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def list_orders(engine, scope, *, statuses=None) -> list[dict]:
    where = (
        (futu_orders.c.broker == scope.broker)
        & (futu_orders.c.account_env == scope.account_env)
        & (futu_orders.c.broker_account == scope.broker_account)
    )
    if statuses is not None:
        where = where & (futu_orders.c.status.in_(list(statuses)))
    stmt = sa.select(futu_orders).where(where).order_by(futu_orders.c.updated_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]
```

`insert_order_fees` mirrors the same UPSERT keyed on `(…, futu_order_id)`. `insert_closed_trades` UPSERTs keyed on `(…, futu_close_id)`. `list_closed_trades` mirrors `list_trades` but filters/orders on `closed_at`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_futu_orders_queries.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/futu_history.py scripts/tests/test_futu_orders_queries.py
git commit -m "feat(futu): query helpers for futu_orders/fees/closed_trades"
```

---

## Task 3: FIFO closed-trade lot-matcher (shared engine)

**Files:**

- Create: `src/xenon/api/services/futu_closed_trades.py`
- Modify: `src/xenon/api/services/futu_nav_backfill.py` (re-express `_compute_daily_realized_pnl` over the shared matcher)
- Test: `scripts/tests/test_futu_closed_trades_matcher.py`

**Interfaces:**

- Consumes: trade dicts from `list_trades` (Task in existing module): keys `futu_code`, `ticker`, `quantity`, `price`, `filled_at`, `raw` (`trd_side`), `futu_deal_id`.
- Produces:
  - `@dataclass(frozen=True) ClosedLot` with fields: `futu_close_id: str`, `ticker: str`, `futu_code: str`, `action: str`, `quantity: Decimal`, `cost_basis: Decimal`, `proceeds: Decimal`, `realized_pnl: Decimal`, `opened_at: datetime | None`, `closed_at: datetime`.
  - `match_closed_lots(trades: list[dict]) -> list[ClosedLot]`
  - `closed_lots_to_rows(lots: list[ClosedLot]) -> list[dict]` (shaped for `insert_closed_trades`)

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_futu_closed_trades_matcher.py
from datetime import datetime, timezone
from decimal import Decimal
from xenon.api.services.futu_closed_trades import match_closed_lots


def _t(deal_id, side, qty, price, day):
    return {
        "futu_deal_id": deal_id, "futu_code": "US.QQQ", "ticker": "QQQ",
        "quantity": qty, "price": price,
        "filled_at": datetime(2026, 6, day, tzinfo=timezone.utc),
        "raw": {"trd_side": side},
    }


def test_long_round_trip_realized_pnl():
    lots = match_closed_lots([
        _t("d1", "BUY", 10, 100, 1),
        _t("d2", "SELL", 10, 110, 2),
    ])
    assert len(lots) == 1
    lot = lots[0]
    assert lot.action == "SELL"
    assert lot.quantity == Decimal("10")
    assert lot.realized_pnl == Decimal("100")  # (110-100)*10*1
    assert lot.cost_basis == Decimal("1000")
    assert lot.proceeds == Decimal("1100")
    assert lot.opened_at.day == 1 and lot.closed_at.day == 2


def test_option_multiplier_applied():
    lots = match_closed_lots([
        {**_t("d1", "BUY", 1, 3.48, 1), "futu_code": "US.QQQ250620C500000", "ticker": "QQQ250620C500000"},
        {**_t("d2", "SELL", 1, 10.40, 2), "futu_code": "US.QQQ250620C500000", "ticker": "QQQ250620C500000"},
    ])
    assert lots[0].realized_pnl == Decimal("692.00")  # (10.40-3.48)*1*100


def test_short_round_trip():
    lots = match_closed_lots([
        _t("d1", "SELL_SHORT", 5, 50, 1),
        _t("d2", "BUY_BACK", 5, 40, 2),
    ])
    assert lots[0].action == "BUY"
    assert lots[0].realized_pnl == Decimal("50")  # (50-40)*5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_futu_closed_trades_matcher.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the matcher**

Lift the queue logic out of `futu_nav_backfill._compute_daily_realized_pnl` (already read at lines 100–150). Each close emits a `ClosedLot`; `futu_close_id` = the closing deal id plus the matched-lot index (stable across re-pulls because deal ids are stable). Reuse `_contract_multiplier` and `_raw_trd_side` (move them into `futu_closed_trades.py`; re-import them in `futu_nav_backfill.py` to keep one definition).

```python
from __future__ import annotations
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

_OCC_TAIL = re.compile(r"\d{6}[CP]\d+$")

def _contract_multiplier(ticker: str) -> int:
    return 100 if _OCC_TAIL.search(ticker) else 1

def _raw_trd_side(trade: dict) -> str:
    raw = trade.get("raw") or {}
    return raw.get("trd_side") or trade.get("action")

@dataclass(frozen=True)
class ClosedLot:
    futu_close_id: str
    ticker: str
    futu_code: str
    action: str
    quantity: Decimal
    cost_basis: Decimal
    proceeds: Decimal
    realized_pnl: Decimal
    opened_at: datetime | None
    closed_at: datetime

def match_closed_lots(trades: list[dict]) -> list[ClosedLot]:
    longs: dict[str, deque] = defaultdict(deque)   # (qty, price, opened_at, deal_id)
    shorts: dict[str, deque] = defaultdict(deque)
    out: list[ClosedLot] = []
    for t in trades:
        code, ticker = t["futu_code"], t["ticker"]
        mult = Decimal(_contract_multiplier(ticker))
        qty, price = Decimal(str(t["quantity"])), Decimal(str(t["price"]))
        when = t["filled_at"].astimezone(timezone.utc)
        side = _raw_trd_side(t)
        if side == "BUY":
            longs[code].append((qty, price, when))
        elif side == "SELL_SHORT":
            shorts[code].append((qty, price, when))
        elif side in ("SELL", "BUY_BACK"):
            book = longs[code] if side == "SELL" else shorts[code]
            remaining, idx = qty, 0
            while remaining > 0 and book:
                lot_qty, lot_price, lot_when = book[0]
                matched = min(lot_qty, remaining)
                if side == "SELL":
                    cost_basis, proceeds = lot_price * matched * mult, price * matched * mult
                    realized = proceeds - cost_basis
                    action = "SELL"
                else:  # BUY_BACK closes a short
                    proceeds, cost_basis = lot_price * matched * mult, price * matched * mult
                    realized = proceeds - cost_basis
                    action = "BUY"
                out.append(ClosedLot(
                    futu_close_id=f"{t['futu_deal_id']}:{idx}",
                    ticker=ticker, futu_code=code, action=action, quantity=matched,
                    cost_basis=cost_basis, proceeds=proceeds, realized_pnl=realized,
                    opened_at=lot_when, closed_at=when,
                ))
                idx += 1
                if matched == lot_qty:
                    book.popleft()
                else:
                    book[0] = (lot_qty - matched, lot_price, lot_when)
                remaining -= matched
    return out

def closed_lots_to_rows(lots: list[ClosedLot]) -> list[dict]:
    return [{
        "futu_close_id": l.futu_close_id, "ticker": l.ticker, "futu_code": l.futu_code,
        "structure": None, "action": l.action, "quantity": l.quantity,
        "entry_cost": l.cost_basis, "exit_cost": l.proceeds,
        "realized_pnl": l.realized_pnl, "cost_basis": l.cost_basis, "proceeds": l.proceeds,
        "opened_at": l.opened_at, "closed_at": l.closed_at, "metadata": {},
    } for l in lots]
```

- [ ] **Step 4: Re-express the NAV daily-sum over the matcher**

In `futu_nav_backfill.py`, replace the body of `_compute_daily_realized_pnl` so it sums `match_closed_lots(trades)` by `closed_at.date()`:

```python
from xenon.api.services.futu_closed_trades import match_closed_lots

def _compute_daily_realized_pnl(trades: list[dict]) -> dict[date, Decimal]:
    daily: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for lot in match_closed_lots(trades):
        daily[lot.closed_at.date()] += lot.realized_pnl
    return dict(daily)
```

- [ ] **Step 5: Run tests (matcher + existing NAV regression)**

Run: `uv run pytest scripts/tests/test_futu_closed_trades_matcher.py scripts/tests/test_futu_nav_backfill.py -v`
Expected: PASS — both the new matcher tests and the existing NAV tests (no drift).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/services/futu_closed_trades.py src/xenon/api/services/futu_nav_backfill.py scripts/tests/test_futu_closed_trades_matcher.py
git commit -m "feat(futu): shared FIFO lot-matcher feeding NAV + closed trades"
```

---

## Task 4: FutuClient order fetchers

**Files:**

- Modify: `src/xenon/clients/futu_client.py`
- Test: `scripts/tests/test_futu_orders_client.py`

**Interfaces:**

- Consumes: `self._trd_ctx.order_list_query`, `self._trd_ctx.history_order_list_query`, `self._trd_ctx.order_fee_query`; existing helpers `_ensure_connected`, `_iter_windows`, `_fmt_futu_ts`, `_parse_futu_ts`, `self._matched_trd_env`, `self._acc_id`.
- Produces:
  - `fetch_open_orders() -> list[dict]` — rows shaped for `insert_orders` (status in the live/working set).
  - `fetch_history_orders(start, end) -> list[dict]` — rows shaped for `insert_orders`.
  - `fetch_order_fees(order_ids: list[str]) -> list[dict]` — rows shaped for `insert_order_fees`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_futu_orders_client.py
import pandas as pd
from unittest.mock import MagicMock
from xenon.clients.futu_client import FutuClient


def _client_with_frame(frame):
    c = FutuClient()
    c._connected = True
    c._acc_id = 1
    c._matched_trd_env = "REAL"
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, frame)  # RET_OK == 0
    c._trd_ctx = ctx
    return c


def test_fetch_open_orders_normalizes_row(monkeypatch):
    monkeypatch.setattr("xenon.clients.futu_client.RET_OK", 0, raising=False)
    frame = pd.DataFrame([{
        "order_id": "O1", "code": "US.QQQ", "trd_side": "BUY",
        "order_type": "NORMAL", "qty": 1, "price": 630.96, "aux_price": 0.0,
        "order_status": "SUBMITTED", "time_in_force": "GTC", "dealt_qty": 0,
        "dealt_avg_price": 0.0, "create_time": "2026-06-17 09:30:00",
        "updated_time": "2026-06-17 09:31:00",
    }])
    c = _client_with_frame(frame)
    rows = c.fetch_open_orders()
    assert rows[0]["futu_order_id"] == "O1"
    assert rows[0]["action"] == "BUY"
    assert rows[0]["order_type"] == "NORMAL"
    assert rows[0]["status"] == "SUBMITTED"
    assert rows[0]["tif"] == "GTC"
    assert rows[0]["limit_price"] == 630.96
    assert rows[0]["ticker"] == "QQQ"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_futu_orders_client.py -v`
Expected: FAIL — `AttributeError: 'FutuClient' object has no attribute 'fetch_open_orders'`.

- [ ] **Step 3: Implement the fetchers**

Mirror `fetch_history_deals` (read at lines 675–752). Field names per the futu SDK `order_list_query` frame: `order_id, code, trd_side, order_type, qty, price, aux_price, order_status, time_in_force, dealt_qty, dealt_avg_price, create_time, updated_time`. Normalize side (`SELL_SHORT→SELL`, `BUY_BACK→BUY`) exactly as `fetch_history_deals` does. Add a `_normalize_order_row(row)` private helper shared by `fetch_open_orders` and `fetch_history_orders`:

```python
# Live/working statuses for the OPEN ORDERS surface.
OPEN_ORDER_STATUSES = ("SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART")
FEE_THROTTLE_SEC = 0.2

def fetch_open_orders(self) -> list[dict]:
    self._ensure_connected()
    from futu import RET_OK, TrdEnv
    env_enum = getattr(TrdEnv, self._matched_trd_env or self.trd_env, TrdEnv.REAL)
    ret, data = self._trd_ctx.order_list_query(acc_id=self._acc_id, trd_env=env_enum)
    if ret != RET_OK:
        raise classify_futu_exception(Exception(str(data)))
    if data is None or data.empty:
        return []
    return [self._normalize_order_row(r) for _, r in data.iterrows()]

def fetch_history_orders(self, start, end) -> list[dict]:
    # Mirror fetch_history_deals window-iteration + throttle, calling
    # self._trd_ctx.history_order_list_query(code="", start=..., end=..., trd_env=..., acc_id=...).
    ...

def _normalize_order_row(self, row) -> dict:
    code = str(row.get("code", ""))
    market, _, ticker = code.partition(".")
    if not ticker:
        ticker, market = code, ""
    raw_side = str(row.get("trd_side", "")).upper()
    action = {"BUY": "BUY", "SELL": "SELL", "SELL_SHORT": "SELL", "BUY_BACK": "BUY"}.get(raw_side, "BUY")
    price = float(row.get("price", 0) or 0)
    return {
        "futu_order_id": str(row.get("order_id")),
        "ticker": ticker, "futu_code": code, "market": market, "action": action,
        "order_type": str(row.get("order_type", "NORMAL")),
        "quantity": float(row.get("qty", 0) or 0),
        "limit_price": price if price > 0 else None,
        "aux_price": (float(row.get("aux_price")) if row.get("aux_price") else None),
        "status": str(row.get("order_status", "")),
        "tif": str(row.get("time_in_force", "DAY")),
        "filled_qty": float(row.get("dealt_qty", 0) or 0),
        "avg_fill_price": (float(row.get("dealt_avg_price")) if row.get("dealt_avg_price") else None),
        "created_at": self._parse_futu_ts(row.get("create_time")),
        "updated_at": self._parse_futu_ts(row.get("updated_time") or row.get("create_time")),
        "raw": {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()},
    }
```

`fetch_order_fees(order_ids)` calls `self._trd_ctx.order_fee_query(order_id_list=..., acc_id=..., trd_env=...)` with a `time.sleep(FEE_THROTTLE_SEC)` between calls; returns `[{"futu_order_id", "total_fee", "currency": "USD", "raw"}]`. If the SDK lacks `order_fee_query` in this version, return `[]` and log once (verify against the installed SDK during implementation — see Task 0 note below).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_futu_orders_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/clients/futu_client.py scripts/tests/test_futu_orders_client.py
git commit -m "feat(futu): client fetchers for open/history orders + fees"
```

> **Task 0 note (do at implementation start):** verify the installed SDK frame columns and that `order_fee_query` exists:
> `cd .venv/lib/python3.13/site-packages/futu && grep -n "def order_list_query\|def order_fee_query\|def history_order_list_query" trade/open_trade_context.py` and inspect the returned DataFrame columns. Adjust field names in `_normalize_order_row` to match the real frame. Do NOT guess column names — read them.

---

## Task 5: Journal auto-import for Futu

**Files:**

- Modify: `src/xenon/db/queries/journal.py`
- Modify: `src/xenon/db/schema.py` (add Futu partial-unique index) + extend the Task-1 migration
- Test: `scripts/tests/test_futu_journal_auto_import.py`

**Interfaces:**

- Consumes: `journal_entries` table; a closed-trade row dict (from Task 3).
- Produces: `upsert_futu_auto_import_entry(conn, *, scope, closed_trade: dict) -> dict | None` — idempotent on `(scope, metadata->>'futu_close_id')`.

- [ ] **Step 1: Add the Futu dedup index to `schema.py` + migration**

```python
# in journal_entries Table(...), add alongside uq_journal_auto_import:
Index(
    "uq_journal_futu_auto_import",
    "broker", "account_env", "broker_account",
    text("(metadata->>'futu_close_id')"),
    unique=True,
    postgresql_where=text("decision = 'FUTU_AUTO_IMPORT'"),
),
```

Add the matching `op.create_index(..., postgresql_where=...)` to `2026_06_17_futu_orders.py`.

- [ ] **Step 2: Write the failing test**

```python
# scripts/tests/test_futu_journal_auto_import.py
import pytest
from datetime import datetime, timezone
from xenon.db.engine import get_sync_engine
from xenon.db.queries.journal import upsert_futu_auto_import_entry, list_journal_entries
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="281753263")
CT = {
    "futu_close_id": "d2:0", "ticker": "QQQ", "realized_pnl": 692.0,
    "cost_basis": 3.48, "proceeds": 1040.0, "quantity": 1,
    "opened_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    "closed_at": datetime(2026, 6, 15, tzinfo=timezone.utc),
}

def test_futu_auto_import_is_idempotent(pg_sync_conn):
    a = upsert_futu_auto_import_entry(pg_sync_conn, scope=SCOPE, closed_trade=CT)
    b = upsert_futu_auto_import_entry(pg_sync_conn, scope=SCOPE, closed_trade=CT)
    assert a is not None
    rows = list_journal_entries(pg_sync_conn, SCOPE, cutoff=None, limit=100)
    futu = [r for r in rows if r.get("decision") == "FUTU_AUTO_IMPORT"]
    assert len(futu) == 1  # second call deduped
    assert futu[0]["metadata"]["futu_close_id"] == "d2:0"
    assert futu[0]["metadata"]["realized_pnl"] == 692.0
```

> Match the `pg_sync_conn`/`list_journal_entries` signatures used in `src/xenon/api/tests/test_journal_auto_import.py`.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_futu_journal_auto_import.py -v`
Expected: FAIL — import error.

- [ ] **Step 4: Implement `upsert_futu_auto_import_entry`**

Sibling of `upsert_auto_import_entry` (read at journal.py:156). `trade_id` stays NULL; scope + closed-trade detail come from the caller (Futu has no `trades` row to resolve from).

```python
def upsert_futu_auto_import_entry(conn, *, scope, closed_trade: dict):
    meta = {
        "source": "futu_closed_trade",
        "futu_close_id": closed_trade["futu_close_id"],
        "realized_pnl": float(closed_trade["realized_pnl"]),
        "cost_basis": float(closed_trade["cost_basis"]),
        "proceeds": float(closed_trade["proceeds"]),
        "quantity": float(closed_trade["quantity"]),
        "opened_at": closed_trade["opened_at"].isoformat() if closed_trade.get("opened_at") else None,
        "close_date": closed_trade["closed_at"].isoformat(),
    }
    stmt = (
        pg_insert(journal_entries)
        .values(
            trade_id=None, ticker=closed_trade["ticker"], decision="FUTU_AUTO_IMPORT",
            authored_by="system", metadata=meta,
            broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account,
            authored_at=closed_trade["closed_at"],
        )
        .on_conflict_do_nothing(
            index_elements=["broker", "account_env", "broker_account", text("(metadata->>'futu_close_id')")],
            index_where=text("decision = 'FUTU_AUTO_IMPORT'"),
        )
        .returning(journal_entries)
    )
    inserted = conn.execute(stmt).first()
    if inserted is not None:
        return journal_entry_to_payload(inserted)
    existing = conn.execute(
        select(journal_entries).where(
            journal_entries.c.decision == "FUTU_AUTO_IMPORT",
            journal_entries.c.broker == scope.broker,
            journal_entries.c.account_env == scope.account_env,
            journal_entries.c.broker_account == scope.broker_account,
            journal_entries.c.metadata["futu_close_id"].astext == closed_trade["futu_close_id"],
        ).limit(1)
    ).first()
    return journal_entry_to_payload(existing) if existing is not None else None
```

> Confirm `on_conflict_do_nothing` accepts a `text()` expression index element on this SQLAlchemy version; if not, target the index by name via `index_elements` workaround or `ON CONFLICT ON CONSTRAINT uq_journal_futu_auto_import`. Verify during implementation.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_futu_journal_auto_import.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/queries/journal.py src/xenon/db/schema.py src/xenon/db/migrations/versions/2026_06_17_futu_orders.py scripts/tests/test_futu_journal_auto_import.py
git commit -m "feat(futu): FUTU_AUTO_IMPORT journal upsert + dedup index"
```

---

## Task 6: Sync service — persist orders/fees/closed-trades + journal

**Files:**

- Modify: `src/xenon/api/services/futu_history_sync.py`
- Test: `scripts/tests/test_futu_orders_sync.py`

**Interfaces:**

- Consumes: Tasks 2–5 (`insert_orders`, `insert_order_fees`, `insert_closed_trades`, `match_closed_lots`/`closed_lots_to_rows`, `upsert_futu_auto_import_entry`), client fetchers (Task 4), `is_read_only()`.
- Produces: `sync_futu_orders(engine, client, scope) -> dict` (counts) wired into the existing backfill entrypoint.

- [ ] **Step 1: Write the failing test** (mock the client; assert rows land + journal deduped + read-only no-ops). Use the existing `test_futu_history_sync.py` harness style.

- [ ] **Step 2: Run → fail.** `uv run pytest scripts/tests/test_futu_orders_sync.py -v`

- [ ] **Step 3: Implement `sync_futu_orders`** — pull open + history orders → `insert_orders`; fees → `insert_order_fees`; build closed-trades from `list_trades` (already-persisted fills) via `match_closed_lots` → `insert_closed_trades`; then for each closed-trade row call `upsert_futu_auto_import_entry`. Guard the whole function with `if is_read_only(): return {...zeros...}`. Wire it into `backfill_history_sync` after trades/cashflows persist (so `futu_trades` exists before closed-trade reconstruction).

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** — `feat(futu): sync persists orders, fees, closed trades + journal`

---

## Task 7: Broker-aware scope dependency

**Files:**

- Modify: `src/xenon/api/guards.py` (rename `get_performance_scope` → `get_broker_scope`; keep behavior)
- Modify: `src/xenon/api/routes/performance.py` (update import/usage)
- Test: `src/xenon/api/tests/test_broker_scope.py`

- [ ] **Step 1: Failing test** — assert `get_broker_scope` resolves IB from app.state by default and FUTU when `broker="FUTU"` (mock `_get_futu_client`).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3:** Rename the function; add `get_performance_scope = get_broker_scope` alias temporarily so nothing breaks mid-refactor, then update the performance route to import `get_broker_scope`; remove the alias.
- [ ] **Step 4: Run → pass** (+ `uv run pytest src/xenon/api/tests/test_performance*.py`).
- [ ] **Step 5: Commit** — `refactor(api): generalize performance scope dep to get_broker_scope`

---

## Task 8: `/orders` FUTU branch + Futu shapers + broker-aware refresh

**Files:**

- Modify: `src/xenon/api/routes/orders.py`
- Test: `scripts/tests/test_orders_futu_branch.py`

**Interfaces:**

- Consumes: `list_orders`, `list_trades` (async — wrap with the sync engine pattern the module already uses), `get_broker_scope`.
- Produces: `orders_payload_for_scope(scope)` returns the IB-identical contract for FUTU scope; `_futu_open_order(row)` and `_futu_executed_order(row)` shapers.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_orders_futu_branch.py — seed futu_orders + futu_trades, assert shape parity
def test_orders_payload_futu_shapes_like_ib(seed_futu_order_and_fill):
    from xenon.api.routes.orders import orders_payload_for_scope
    from xenon.execution.account_scope import AccountScope
    scope = AccountScope(broker="FUTU", account_env="live", broker_account="281753263")
    payload = orders_payload_for_scope(scope)
    oo = payload["open_orders"][0]
    assert set(oo) >= {"symbol", "action", "orderType", "totalQuantity", "limitPrice", "status", "tif"}
    assert oo["orderType"] == "LMT"   # NORMAL → LMT
    assert oo["status"] == "Submitted"  # SUBMITTED → Submitted
    assert oo["tif"] == "GTC"
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement the branch.** At the top of `orders_payload_for_scope`, branch on `scope.broker`:

```python
def orders_payload_for_scope(scope, *, limit=200):
    if scope.broker == "FUTU":
        return _futu_orders_payload(scope, limit=limit)
    ...  # existing IB body unchanged
```

`_futu_orders_payload` reads `futu_orders` (statuses in `OPEN_ORDER_STATUSES`) + `futu_trades` (today's ET fills) via the sync engine, mapping with:

```python
_FUTU_STATUS = {
    "SUBMITTING": "PendingSubmit", "WAITING_SUBMIT": "PendingSubmit",
    "SUBMITTED": "Submitted", "FILLED_PART": "PartiallyFilled",
    "FILLED_ALL": "Filled", "CANCELLED_ALL": "Cancelled", "CANCELLED_PART": "Cancelled",
}
_FUTU_TYPE = {"NORMAL": "LMT", "MARKET": "MKT"}  # else pass through label

def _futu_open_order(row) -> dict:
    return {
        "submissionId": row["futu_order_id"], "orderId": 0, "permId": 0,
        "symbol": row["ticker"],
        "contract": {"conId": None, "symbol": row["ticker"], "secType": "STK",
                     "strike": None, "right": None, "expiry": None},
        "action": row["action"],
        "orderType": _FUTU_TYPE.get(row["order_type"], row["order_type"]),
        "totalQuantity": int(row["quantity"]),
        "limitPrice": _float_or_none(row.get("limit_price")),
        "auxPrice": _float_or_none(row.get("aux_price")),
        "status": _FUTU_STATUS.get(row["status"], row["status"]),
        "filled": int(row.get("filled_qty") or 0),
        "remaining": max(int(row["quantity"]) - int(row.get("filled_qty") or 0), 0),
        "avgFillPrice": _float_or_none(row.get("avg_fill_price")),
        "tif": str(row.get("tif") or "DAY"), "modifySequence": 0,
    }
```

`_futu_executed_order(row)` maps a `futu_trades` row to the `ExecutedOrder` shape (side `BOT`/`SLD`, qty, price, commission from fees, `time`). Option-symbol display via the existing `_display_symbol` when the OCC tail is present.

- [ ] **Step 4:** Make `/orders/refresh` broker-aware — read `broker` query param; `FUTU` → call the Task-6 sync (orders pull into DB), `IB` → existing IB refresh. Switch the route deps from `get_account_scope` to `get_broker_scope`.

- [ ] **Step 5: Run → pass.**

- [ ] **Step 6: Commit** — `feat(api): /orders FUTU branch with IB-unified shape`

---

## Task 9: `/blotter` FUTU branch (30-day historical trades)

**Files:**

- Modify: `src/xenon/api/routes/blotter.py`
- Test: `scripts/tests/test_blotter_futu_branch.py`

- [ ] **Step 1: Failing test** — seed `futu_closed_trades`, call the blotter handler with FUTU scope, assert `closed_trades` rows carry `realized_pnl`, `cost_basis`, `proceeds`, `date`, `symbol`, `side`, `qty` (matching the IB blotter response keys the frontend reads).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3:** Add `scope.broker == "FUTU"` branch that reads `list_closed_trades` and shapes to the existing blotter response contract (inspect `blotter.py` for the exact IB `closed_trades` row keys and mirror them). Switch the route dep to `get_broker_scope`. The `/journal` route only needs its dep switched to `get_broker_scope` (the FUTU rows already exist from Task 6) — fold that one-line change in here with a journal-scope test.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(api): /blotter + /journal FUTU scope`

---

## Task 10: Next API routes forward `?broker=`

**Files:**

- Modify: `web/app/api/orders/route.ts`, `web/app/api/blotter/route.ts`, `web/app/api/journal/route.ts`
- Test: `web/tests/api-orders-broker-param.test.ts`

**Interfaces:**

- Produces: each route reads `?broker=` from the request URL and appends it to the `xenonFetch` path; default omits it (IB).

- [ ] **Step 1: Failing Vitest** — mock `xenonFetch`; call `GET(new Request("http://x/api/orders?broker=FUTU"))`; assert `xenonFetch` was called with `"/orders?broker=FUTU"`.

- [ ] **Step 2: Run → fail.** `cd web && npx vitest run tests/api-orders-broker-param.test.ts`

- [ ] **Step 3: Implement.** Change each handler to accept `req: Request`, e.g.:

```ts
export async function GET(req: Request): Promise<Response> {
  const broker = new URL(req.url).searchParams.get("broker");
  const qs = broker ? `?broker=${encodeURIComponent(broker)}` : "";
  const data = await xenonFetch(`/orders${qs}`, {
    method: "GET",
    timeout: 10_000,
  });
  return NextResponse.json(data);
}
```

For `/api/orders` POST, forward the same `?broker=` to `/orders/refresh`. For `/api/blotter`, forward to both GET and POST. For `/api/journal`, forward on GET.

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(web): forward broker scope through orders/blotter/journal API routes`

---

## Task 11: Thread `activeAccount` into the data hooks

**Files:**

- Modify: `web/lib/useOrders.ts`, `web/lib/useBlotter.ts`, `web/lib/useJournal.ts`
- Modify: `web/components/WorkspaceShell.tsx:132`, `web/components/WorkspaceSections.tsx:957,2243`
- Test: `web/tests/use-orders-broker.test.tsx`

**Interfaces:**

- Produces: `useOrders(active, broker?)`, `useBlotter(active, broker?)`, `useJournal(active, broker?)` where `broker: "IB" | "FUTU"` (default `"IB"`); the broker is appended to every fetch URL.

- [ ] **Step 1: Failing Vitest** — render `useOrders(true, "FUTU")`, assert `fetch` called with `/api/orders?broker=FUTU`.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement.**
  - `useOrders`: add `broker: "IB" | "FUTU" = "IB"` param; build `const q = broker === "FUTU" ? "?broker=FUTU" : "";` and use `/api/orders${q}` in both GET and POST; add `broker` to the `useEffect` deps + reset `didInitialSync`/`data` when `broker` changes (so switching tabs refetches).
  - `useBlotter` / `useJournal`: pass a broker-suffixed endpoint into `useSyncHook` (`endpoint: broker === "FUTU" ? "/api/blotter?broker=FUTU" : "/api/blotter"`). The module-level cache keys on endpoint string, so per-broker isolation is automatic.
  - Call sites: `WorkspaceShell.tsx:132` → `useOrders(shouldAutoSyncOrders, activeAccount === "futu" ? "FUTU" : "IB")`. In `WorkspaceSections.tsx`, thread the existing `activeAccount` prop into `useBlotter(true, …)` (line 2243) and `useJournal(…)` (line 957). (`activeAccount` is already passed into these components per the explore findings.)

- [ ] **Step 4: Run → pass** (`cd web && npx vitest run tests/use-orders-broker.test.tsx`).
- [ ] **Step 5: Commit** — `feat(web): render Futu orders/blotter/journal when Futu account active`

---

## Task 12: Full suite + E2E gate

- [ ] **Step 1:** `uv run python scripts/infra/dev/run_pytest_affected.py` → all green.
- [ ] **Step 2:** `cd web && npm test` (Vitest) → green; `npm run typecheck && npm run lint` → clean.
- [ ] **Step 3:** Boot the dev stack (paper) and verify in the browser (handled in the execution-phase verification, not a unit step): switch to the Futu account → OPEN ORDERS / EXECUTED / HISTORICAL / JOURNAL populate; order buttons absent on Futu rows.
- [ ] **Step 4: Commit** any test-only fixups — `test(futu): suite green for Futu orders feature`.

---

## Self-Review (completed against the spec)

- **Spec §"OPEN ORDERS"** → Tasks 4 (fetch), 6 (persist + poll), 8 (shape). ✔
- **Spec §"TODAY'S EXECUTED ORDERS"** → reuses `futu_trades`; Task 8 `_futu_executed_order`. ✔
- **Spec §"HISTORICAL TRADES (30 DAYS)"** → Tasks 3 (FIFO), 6 (persist), 9 (blotter branch). ✔
- **Spec §"TRADE JOURNAL"** → Tasks 3, 5, 6, 9. ✔
- **Spec §"unify with IB"** → Tasks 7–9 keep the identical response contract; no new types. ✔
- **Spec §"order types #5"** → Task 8 `_FUTU_TYPE` map + pass-through; Task 4 stores raw label. ✔
- **Spec §"DB-first / read-only"** → Tasks 6/8 guard with `is_read_only()`; reads are Postgres-only. ✔
- **Spec §"poll cadence 60s"** → Task 6 open-orders poll (server.py loop). ✔
- **Type consistency:** `futu_close_id` is the dedup key in Tasks 1/3/5; `OPEN_ORDER_STATUSES` defined in Task 4 and consumed in Task 8; `get_broker_scope` defined in Task 7 and consumed in Tasks 8/9.
- **Open verification items flagged inline** (Task 0 note, `order_fee_query` existence, `on_conflict` with `text()` index element) — to confirm against the installed SDK / SQLAlchemy during implementation, never guessed.
