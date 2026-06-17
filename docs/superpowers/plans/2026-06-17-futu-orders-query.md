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
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)
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
    # review Pass 2 (P0-1): deterministic order so split-lot ids are stable across
    # re-pulls. list_trades orders only by filled_at; equal-timestamp fills could
    # otherwise reorder and remint different close ids → duplicate journal rows.
    # Secondary key = futu_deal_id (stable, unique per fill).
    trades = sorted(trades, key=lambda t: (t["filled_at"], str(t["futu_deal_id"])))
    longs: dict[str, deque] = defaultdict(deque)   # (qty, price, opened_at, open_deal_id)
    shorts: dict[str, deque] = defaultdict(deque)
    out: list[ClosedLot] = []
    for t in trades:
        code, ticker = t["futu_code"], t["ticker"]
        mult = Decimal(_contract_multiplier(ticker))
        qty, price = Decimal(str(t["quantity"])), Decimal(str(t["price"]))
        when = t["filled_at"].astimezone(timezone.utc)
        deal_id = str(t["futu_deal_id"])
        side = _raw_trd_side(t)
        if side == "BUY":
            longs[code].append((qty, price, when, deal_id))
        elif side == "SELL_SHORT":
            shorts[code].append((qty, price, when, deal_id))
        elif side in ("SELL", "BUY_BACK"):
            book = longs[code] if side == "SELL" else shorts[code]
            remaining = qty
            while remaining > 0 and book:
                lot_qty, lot_price, lot_when, open_deal_id = book[0]
                matched = min(lot_qty, remaining)
                if side == "SELL":
                    cost_basis, proceeds = lot_price * matched * mult, price * matched * mult
                    action = "SELL"
                else:  # BUY_BACK closes a short
                    proceeds, cost_basis = lot_price * matched * mult, price * matched * mult
                    action = "BUY"
                out.append(ClosedLot(
                    # review Pass 2 (P0-1): key on BOTH deal ids so partial fills against
                    # multiple open lots stay unique AND stable (not a positional index).
                    futu_close_id=f"{deal_id}:{open_deal_id}",
                    ticker=ticker, futu_code=code, action=action, quantity=matched,
                    cost_basis=cost_basis, proceeds=proceeds, realized_pnl=proceeds - cost_basis,
                    opened_at=lot_when, closed_at=when,
                ))
                if matched == lot_qty:
                    book.popleft()
                else:
                    book[0] = (lot_qty - matched, lot_price, lot_when, open_deal_id)
                remaining -= matched
            if remaining > 0:
                # review Pass 2 (P1-3): preserve the existing NAV observability — a close
                # with no open lot means a pre-inception position; warn, don't silently skip.
                logger.warning("close with no open lot: side=%s code=%s qty_unmatched=%s deal=%s",
                               side, code, remaining, deal_id)
        else:
            logger.warning("unknown trd_side=%r deal=%s — skipping", side, deal_id)
    return out
```

> **Edge-case tests required (Pass 2):** (a) a `SELL` with no prior `BUY` logs a warning and emits no lot; (b) an unknown `trd_side` logs and is skipped; (c) two fills with the **same** `filled_at` produce the **same** `futu_close_id`s on a re-pull regardless of input order (feed the list shuffled, assert identical ids).

```

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
OPEN_ORDER_STATUSES = ("SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART", "CANCELLING_PART", "CANCELLING_ALL")
# review Pass 2 (P1-6): Futu caps trade queries at 10 req / 30s. Batching 50 ids per
# call keeps the call count low, but throttle 3.5s between chunks (matches DEAL_THROTTLE_SEC)
# so a many-order account can't blow the limit. Do NOT use a sub-second throttle here.
FEE_THROTTLE_SEC = 3.5

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

`fetch_order_fees(order_ids)` — **verified**: `order_fee_query(order_id_list=[...], trd_env=..., acc_id=...)` exists and returns a frame with columns `order_id, fee_amount, fee_details`. It accepts a **batch** list, so chunk `order_ids` (e.g. 50/call) and `time.sleep(FEE_THROTTLE_SEC)` between chunks — not per-order. Map `fee_amount → total_fee`:

```python
def fetch_order_fees(self, order_ids: list[str]) -> list[dict]:
    self._ensure_connected()
    from futu import RET_OK, TrdEnv
    import time as _time
    env_enum = getattr(TrdEnv, self._matched_trd_env or self.trd_env, TrdEnv.REAL)
    out: list[dict] = []
    chunks = [order_ids[i:i + 50] for i in range(0, len(order_ids), 50)]
    for n, chunk in enumerate(chunks):
        if n and self.FEE_THROTTLE_SEC > 0:
            _time.sleep(self.FEE_THROTTLE_SEC)
        ret, data = self._trd_ctx.order_fee_query(order_id_list=chunk, trd_env=env_enum, acc_id=self._acc_id)
        if ret != RET_OK:
            raise classify_futu_exception(Exception(str(data)))
        if data is None or data.empty:
            continue
        for _, row in data.iterrows():
            out.append({
                "futu_order_id": str(row.get("order_id")),
                "total_fee": float(row.get("fee_amount", 0) or 0),
                "currency": "USD",
                "raw": {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()},
            })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_futu_orders_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/clients/futu_client.py scripts/tests/test_futu_orders_client.py
git commit -m "feat(futu): client fetchers for open/history orders + fees"
```

> **SDK columns — VERIFIED 2026-06-17** against `.venv/.../futu/trade/open_trade_context.py`:
>
> - `order_list_query(order_id="", status_filter_list=[], code='', start='', end='', trd_env=, acc_id=, …)` and `history_order_list_query(status_filter_list=[], code='', start='', end='', trd_env=, acc_id=, …)` return the **identical** col_list: `code, stock_name, order_market, trd_side, order_type, order_status, order_id, qty, price, create_time, updated_time, dealt_qty, dealt_avg_price, last_err_msg, remark, time_in_force, fill_outside_rth, session, aux_price, trail_type, trail_value, trail_spread, currency, jp_acc_type`.
> - `order_fee_query(order_id_list=[], trd_env=, acc_id=)` returns `order_id, fee_amount, fee_details`.
> - Enums (verified): `OrderType` = NORMAL/MARKET/ABSOLUTE_LIMIT/AUCTION/AUCTION_LIMIT/SPECIAL_LIMIT/SPECIAL_LIMIT_ALL/STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED/TRAILING_STOP/TRAILING_STOP_LIMIT/TWAP/TWAP_LIMIT/VWAP/VWAP_LIMIT; `TimeInForce` = DAY/GTC; `OrderStatus` = UNSUBMITTED/WAITING_SUBMIT/SUBMITTING/SUBMIT_FAILED/TIMEOUT/SUBMITTED/FILLED_PART/FILLED_ALL/CANCELLING_PART/CANCELLING_ALL/CANCELLED_PART/CANCELLED_ALL/FAILED/DISABLED/DELETED/FILL_CANCELLED.
>   The `_normalize_order_row` field names above match this frame. Still confirm `pd.isna` import (`import pandas as pd` is already module-level in `futu_client.py`).

---

## Task 5: Journal auto-import for Futu

**Files:**

- Modify: `src/xenon/db/queries/journal.py`
- Modify: `src/xenon/db/schema.py` (add Futu partial-unique index) + extend the Task-1 migration
- Test: `scripts/tests/test_futu_journal_auto_import.py`

**Interfaces:**

- Consumes: `journal_entries` table; a closed-trade row dict (from Task 3).
- Produces: `upsert_futu_auto_import_entry(conn, *, scope, closed_trade: dict) -> dict | None` — idempotent on `(scope, futu_close_id)`.

- [ ] **Step 1: Add a `futu_close_id` column + Futu dedup index to `schema.py` + migration**

Rationale (review-cycle Pass 1): a partial-unique index over a JSONB expression (`metadata->>'futu_close_id'`) makes `ON CONFLICT` brittle — Postgres requires the conflict target to exactly match the indexed expression, and SQLAlchemy's `on_conflict_do_nothing` does not reliably accept a `text()` expression as an `index_element`. A dedicated nullable column is unambiguous and keeps `journal_entry_to_payload` unchanged.

```python
# in journal_entries Table(...): add a column alongside the existing scope columns…
Column("futu_close_id", Text),  # set only for decision='FUTU_AUTO_IMPORT'; NULL for IB rows
# …and an index alongside uq_journal_auto_import:
Index(
    "uq_journal_futu_auto_import",
    "broker", "account_env", "broker_account", "futu_close_id",
    unique=True,
    postgresql_where=text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"),
),
```

Add the matching `op.add_column("journal_entries", sa.Column("futu_close_id", sa.Text()), schema="xenon")` and `op.create_index(..., postgresql_where=sa.text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"))` to `2026_06_17_futu_orders.py`.

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
    # review Pass 2 (P1-7): journal_entry_to_payload LIFTS metadata fields to top-level —
    # it does NOT return a nested {"metadata": {...}}. Assert top-level keys.
    assert a["decision"] == "FUTU_AUTO_IMPORT"
    assert float(a["realized_pnl"]) == 692.0
    # dedup: a query for FUTU rows returns exactly one
    rows = list_journal_entries(pg_sync_conn, scope=SCOPE, cutoff=None, limit=100)
    futu = [r for r in rows if r.get("decision") == "FUTU_AUTO_IMPORT"]
    assert len(futu) == 1  # second call deduped
```

> review Pass 2 (P1-7): **before writing this test**, read the real `journal_entry_to_payload` and `list_journal_entries` in `src/xenon/db/queries/journal.py` + `src/xenon/api/tests/test_journal_auto_import.py`: confirm (a) which metadata keys are lifted top-level (so `_futu_auto_import` metadata uses the SAME keys the IB path lifts — `realized_pnl`, `return_on_risk`, `entry_cost`, `structure`, `quantity` — or the journal table columns won't render for Futu rows), and (b) the exact `list_journal_entries` parameter order / keyword-only-ness. Adjust the assertions to the real payload shape.

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
            authored_by="system", metadata=meta, futu_close_id=closed_trade["futu_close_id"],
            broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account,
            authored_at=closed_trade["closed_at"],
        )
        .on_conflict_do_nothing(
            index_elements=["broker", "account_env", "broker_account", "futu_close_id"],
            index_where=text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"),
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
            journal_entries.c.futu_close_id == closed_trade["futu_close_id"],
        ).limit(1)
    ).first()
    return journal_entry_to_payload(existing) if existing is not None else None
```

> Plain-column `ON CONFLICT` — no expression-index gamble. `meta` should also carry the keys the frontend journal row reader expects (`realized_pnl`, `return_on_risk`, `entry_cost`, `structure`, `quantity`); during implementation, read `journal_entry_to_payload` + the `JournalSections` row mapper in `web/components/WorkspaceSections.tsx` and align metadata keys so the QTY / ENTRY COST / REALIZED P&L / ROR columns render for Futu rows.

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
- Modify: `src/xenon/api/server.py` (open-orders poll loop + `/futu/sync` & `/orders/refresh?broker=FUTU` call `sync_futu_orders`)
- Test: `scripts/tests/test_futu_orders_sync.py`, `scripts/tests/test_futu_orders_poll_smoke.py`

**Interfaces:**

- Consumes: Tasks 2–5 (`insert_orders`, `insert_order_fees`, `insert_closed_trades`, `match_closed_lots`/`closed_lots_to_rows`, `upsert_futu_auto_import_entry`), client fetchers (Task 4), existing `fetch_history_deals` + `insert_trades`, `is_read_only()`.
- Produces: `sync_futu_orders(engine, client, scope, *, since=None) -> dict` (counts) — `since=None` means "today only" (the 60s poll); the nightly loop/CLI pass the full incremental watermark. Wired into the existing backfill entrypoint; `_maybe_start_futu_orders_poll()` in server.py.

- [ ] **Step 1: Write the failing test** (mock the client; assert orders/fees/closed-trades land + journal deduped + **today's deals refreshed into `futu_trades`** + read-only no-ops). Use the existing `test_futu_history_sync.py` harness style.

- [ ] **Step 2: Run → fail.** `uv run pytest scripts/tests/test_futu_orders_sync.py -v`

- [ ] **Step 3: Implement `sync_futu_orders`** — in order:
  1. `if is_read_only(): return {…zeros…}` (guard first).
  2. **Acquire a per-scope singleflight lock** (review Pass 2, P0-2). The 60s poll, the nightly 16:30 loop, `POST /futu/sync`, and `/orders/refresh?broker=FUTU` can all call this concurrently. Base UPSERTs are safe, but step 5 rebuilds _derived_ `futu_closed_trades` from the full `list_trades` set and step 6 writes journal rows — two overlapping runs could rebuild from divergent snapshots or double-insert. Use an `asyncio.Lock` keyed by `(broker, account_env, broker_account)` (mirror `server._get_futu_lock()`), OR a Postgres advisory lock (`pg_advisory_xact_lock(hashtext(scope_key))`) for cross-process safety with the nightly CLI. Prefer the advisory lock since the CLI runs in a separate process.
  3. Refresh **today's fills**: `client.fetch_history_deals(today_et_start, now)` → `insert_trades` (so TODAY'S EXECUTED ORDERS is live, not just nightly). This is the live-fills path the EXECUTED panel reads.
  4. `client.fetch_open_orders()` + `client.fetch_history_orders(since, now)` → `insert_orders`. **review Pass 3 (A3-3):** `since` is a parameter — the 60s poll passes `today_et_start` (narrow window, bounds the call count under Futu's 10 req/30s), while the nightly loop / CLI pass the full incremental watermark. **review Pass 3 (A3-4):** filter to `market == "US"` before persisting (mirror the `futu_trades` policy in `fetch_history_deals`) so non-US orders don't leak into the US-only surface.
  5. `client.fetch_order_fees([o["futu_order_id"] for o in orders])` → `insert_order_fees`.
  6. **In a single transaction** (review Pass 2, P0-2): rebuild closed-trades from `list_trades(engine, scope)` via `match_closed_lots` → `closed_lots_to_rows` → `insert_closed_trades`, then `upsert_futu_auto_import_entry` for each closed-trade row. Wrapping the rebuild + journal upsert in one `engine.begin()` keeps `futu_closed_trades` and the `FUTU_AUTO_IMPORT` journal consistent even if a second run interleaves. Wire into `backfill_history_sync` after trades/cashflows persist (so `futu_trades` exists before closed-trade reconstruction).
  - Add a **concurrency test**: two `sync_futu_orders` calls on the same scope (one awaiting the lock) produce no duplicate `futu_closed_trades` / journal rows.

- [ ] **Step 4: Add `_maybe_start_futu_orders_poll()` to server.py** — mirror `_maybe_start_futu_history_loop`. A 60s loop (env `XENON_FUTU_ORDERS_POLL_SEC`, default 60; disable with `XENON_FUTU_ORDERS_POLL=0`) that, **only during RTH** (reuse the market-hours helper) and **only when not read-only**, resolves the FUTU scope via the same `_scope_factory` the history loop uses and calls `sync_futu_orders`. Skipped in test mode and under `XENON_READ_ONLY=1` (mirror the existing `_maybe_start_*` guards). Make `/futu/sync` and `/orders/refresh?broker=FUTU` also call `sync_futu_orders` so the manual refresh button pulls fresh.

- [ ] **Step 5: Run → pass** (`scripts/tests/test_futu_orders_sync.py` + a loop smoke test mirroring `test_futu_history_loop_smoke.py`).

- [ ] **Step 6: Commit** — `feat(futu): sync + 60s RTH poll persist orders, fees, closed trades, fills, journal`

---

## Task 7: Broker-aware scope dependency

**Files:**

- Modify: `src/xenon/api/guards.py` (rename `get_performance_scope` → `get_broker_scope`; keep behavior)
- Modify: `src/xenon/api/routes/performance.py` (update import/usage)
- Test: `src/xenon/api/tests/test_broker_scope.py`

- [ ] **Step 1: Failing test** — assert `get_broker_scope` resolves IB from app.state by default and FUTU when `broker="FUTU"` (mock `_get_futu_client`). Add a test: when `broker="FUTU"` **and OpenD raises `FutuConnectionError`**, the scope still resolves from the DB fallback (seed a `futu_orders`/`futu_trades` row, assert the returned scope matches) — no 503.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3:** Rename the function; add `get_performance_scope = get_broker_scope` alias temporarily so nothing breaks mid-refactor, then update the performance route to import `get_broker_scope`; remove the alias.
- [ ] **Step 4 (review Pass 3, A3-1 — DB-first resilience):** The current FUTU branch connects to OpenD live and raises 503 when OpenD is down — which would blank the Futu ORDERS/BLOTTER/JOURNAL pages even though the data is already in Postgres. That violates the DB-first requirement on the read path. Add a fallback: when the live connect raises `FutuConnectionError`, resolve the FUTU scope from the **last-synced scope in the DB** (e.g. `SELECT account_env, broker_account FROM xenon.futu_orders WHERE broker='FUTU' ORDER BY updated_at DESC LIMIT 1`, falling back to `futu_trades`, then `account_snapshots`). Only raise 503 if no prior Futu data exists at all (genuinely never synced). Keep the live path first (it's authoritative); the DB fallback is the degrade. This also hardens the existing performance route for free.
- [ ] **Step 5: Run → pass** (+ `uv run pytest src/xenon/api/tests/test_performance*.py`).
- [ ] **Step 6: Commit** — `refactor(api): broker-aware scope dep with DB fallback when OpenD is down`

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
    "CANCELLING_PART": "Submitted", "CANCELLING_ALL": "Submitted",
}
_FUTU_TYPE = {"NORMAL": "LMT", "MARKET": "MKT"}  # else pass through label

# review Pass 2 (P1-4): Futu codes can be OCC option symbols (e.g. US.QQQ250620C500000).
# The frontend reads contract.secType/right/strike/expiry to render option rows — so we
# must parse OCC, not hardcode STK. Reuse the OCC tail regex from futu_closed_trades.py.
_OCC = re.compile(r"^(?P<u>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<strike>\d+)$")

def _futu_contract(ticker: str) -> dict:
    m = _OCC.match(ticker)
    if not m:
        return {"conId": None, "symbol": ticker, "secType": "STK",
                "strike": None, "right": None, "expiry": None}
    return {
        "conId": None, "symbol": m["u"], "secType": "OPT",
        "strike": int(m["strike"]) / 1000.0, "right": m["cp"],
        "expiry": f"20{m['y']}-{m['m']}-{m['d']}",
    }

def _futu_open_order(row) -> dict:
    contract = _futu_contract(str(row["ticker"]))
    # review Pass 2 (P1-5) + Pass 3 (A3-2): the frontend keys rows on `${orderId}-${permId}`
    # and the pending-action map is keyed on permId; orderId:0/permId:0 for every row collides
    # as duplicate React keys. Futu order ids are ~19-digit strings — using them as a JS number
    # loses precision past 2^53 and can re-collide. So (a) hash to a bounded <2^53 surrogate here,
    # and (b) Task 11 makes the frontend prefer the string `submissionId` for the React key.
    oid = int(hashlib.sha1(str(row["futu_order_id"]).encode()).hexdigest()[:12], 16)  # < 2^48
    return {
        "submissionId": str(row["futu_order_id"]), "orderId": oid, "permId": oid,
        "symbol": _display_symbol(contract["symbol"], contract["secType"], contract["right"], contract["strike"]),
        "contract": contract,
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

`_futu_executed_order(row)` maps a `futu_trades` row to the `ExecutedOrder` shape (side `BOT`/`SLD`, qty, price, commission from `futu_order_fees`, `time`), using the same `_futu_contract` + `_display_symbol` for option display. Add a unit assertion that an OCC ticker yields `secType=="OPT"` with parsed strike/right/expiry, and that two distinct Futu orders produce distinct `orderId`s.

- [ ] **Step 4:** Make `/orders/refresh` broker-aware. **Note (review Pass 2, P2-8):** `/orders/refresh` is defined in `src/xenon/api/server.py:1446` (`@app.post("/orders/refresh")`, `Depends(get_account_scope)` + `require_mode_verified`), NOT in `routes/orders.py`. The GET `/orders` is in `routes/orders.py`. Switch **both** deps from `get_account_scope` to `get_broker_scope`. In `orders_refresh`, branch: `FUTU` → call the Task-6 `sync_futu_orders`; `IB` → existing IB refresh. (Files for this task therefore include `src/xenon/api/server.py`.)

- [ ] **Step 5: Run → pass.**

- [ ] **Step 6: Commit** — `feat(api): /orders FUTU branch with IB-unified shape`

---

## Task 9: `/blotter` FUTU branch (30-day historical trades)

**Files (review Pass 2, P2-8 — corrected):**

- Modify: `src/xenon/api/server.py` — `/blotter` GET (`server.py:2784 blotter_get`) + POST (`server.py:2712 blotter_sync`); both currently `Depends(get_account_scope)`. There is **no** `routes/blotter.py`.
- Modify: `src/xenon/db/queries/blotter.py` — `fetch_blotter_pg` (the IB shaper); add a `fetch_futu_blotter` sibling (or a `broker` branch) reading `futu_closed_trades` and emitting the same `_trade_to_payload` shape.
- Modify: `src/xenon/api/routes/journal.py` — switch `GET /journal` dep to `get_broker_scope`.
- Test: `scripts/tests/test_blotter_futu_branch.py`

- [ ] **Step 1: Failing test** — seed `futu_closed_trades`, call `blotter_get` (or `fetch_futu_blotter`) with FUTU scope, assert `closed_trades` rows carry `realized_pnl`, `cost_basis`, `proceeds`, plus the keys `_trade_to_payload` emits (`date`/`symbol`/`side`/`qty` — **read `db/queries/blotter.py:_trade_to_payload` for the exact key names** and mirror them; do not invent keys).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3:** In `db/queries/blotter.py`, add the Futu reader shaping `futu_closed_trades` into the existing `_trade_to_payload` contract. For **commission**: `futu_closed_trades` has no per-lot commission; set `commission` to `None`/`0` for Futu rows (the 30-day table's COMMISSION column tolerates this — IB Flex is the only commission-bearing source). In `server.py`, switch `blotter_get` to `get_broker_scope` and branch `scope.broker == "FUTU"` to the Futu reader.
- [ ] **Step 4:** Switch `blotter_sync` (POST) to `get_broker_scope` and branch: `scope.broker == "FUTU"` → call `sync_futu_orders` (rebuilds closed-trades from fills) instead of the IB Flex sync — so the HISTORICAL TRADES "REFRESH" button works on the Futu tab. Then switch `routes/journal.py` `GET /journal` dep to `get_broker_scope` (FUTU rows already exist from Task 6) and add a journal-scope test.
- [ ] **Step 5: Run → pass.**
- [ ] **Step 6: Commit** — `feat(api): /blotter + /journal FUTU scope`

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

- [ ] **Step 4: Hide IB-only controls on the Futu tab.** The TRADE JOURNAL "SYNC IB" button calls `/api/journal/sync` (an IB-only path); the OPEN ORDERS rows render MODIFY/CANCEL actions. When `activeAccount === "futu"`, hide the "SYNC IB" button and the per-row MODIFY/CANCEL/order-entry actions (Futu is read-only). The positions table already renders read-only on Futu — mirror that gate. Add a Vitest asserting the SYNC IB button is absent when `activeAccount="futu"`.

- [ ] **Step 5 (review Pass 3, A3-2): make the OPEN ORDERS row key safe for Futu.** Find where open-order rows are keyed in `WorkspaceSections.tsx` (currently `${orderId}-${permId}`). Futu's hashed surrogate `orderId` is collision-safe but make the key robust by preferring the stable string `submissionId` when present: `key={o.submissionId ?? \`${o.orderId}-${o.permId}\`}`. Same for any `permId`-keyed pending-action map — fall back to `submissionId`. Add a Vitest that two Futu orders render with distinct keys (no React duplicate-key warning).

- [ ] **Step 6: Run → pass** (`cd web && npx vitest run tests/use-orders-broker.test.tsx`).
- [ ] **Step 7: Commit** — `feat(web): render Futu orders/blotter/journal when Futu account active`

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
- **Type consistency:** `futu_close_id` = PK of `futu_closed_trades` (Task 1), `ClosedLot` field + close key `close_deal_id:open_deal_id` (Task 3), and a dedicated `journal_entries` column (Task 5); `OPEN_ORDER_STATUSES` defined Task 4, consumed Task 8; `get_broker_scope` defined Task 7, consumed Tasks 8/9; `sync_futu_orders` defined Task 6, consumed Tasks 8 (`/orders/refresh`) + 9 (`/blotter` POST); `match_closed_lots`/`closed_lots_to_rows` Task 3 → Task 6; `insert_closed_trades`/`list_closed_trades` Task 2 → Tasks 6/9.

### Review-cycle resolutions (verified, not guessed)

- **SDK shapes VERIFIED** against installed `futu-api` 0.137.0: `order_list_query`/`history_order_list_query` identical col_list; `order_fee_query(order_id_list)` → `(order_id, fee_amount, fee_details)`. Enums confirmed.
- **File targets corrected (Pass 2):** `/blotter` GET+POST and `/orders/refresh` live in `src/xenon/api/server.py` (not route files); blotter shaping is in `db/queries/blotter.py`. `/orders` GET and `/journal` GET are in `routes/`.
- **Idempotency (Pass 2/3):** `futu_close_id` = `close_deal_id:open_deal_id` with deterministic `(filled_at, futu_deal_id)` sort → stable across re-pulls; per-scope singleflight lock + single-transaction derived rebuild prevents poll/nightly races.
- **DB-first resilience (Pass 3):** FUTU scope falls back to last-synced DB scope when OpenD is down — read path never 503s on cached data.
- **JS-safe keys (Pass 3):** Futu `orderId` is a bounded SHA-1 surrogate (< 2^48); frontend keys on string `submissionId`.
- **Remaining implementation-time confirmations (not blockers):** exact `journal_entry_to_payload` lifted-metadata keys + `list_journal_entries` signature (Task 5 note); exact `_trade_to_payload` blotter keys (Task 9 note); `import hashlib`/`import re` in `orders.py`. All flagged inline with "read the real code first."
