# Postgres Migration Loose-Ends Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six gaps surfaced by the design-vs-implementation review of `feat/postgres-migration-completion` so the Postgres migration ships with no significant loose ends.

**Architecture:** Six tasks land on top of the existing `feat/postgres-migration-completion` branch (currently at tip `941ad4ab`, rebased on `master`). Task 0 is a prerequisite that plumbs a stable join key (`perm_id`) through both the PG and Flex blotter payloads — Tasks 3 and 5 depend on it. Task 1 (listener) is the only silent regression in the migration; it runs early but is independent of Task 0. Tasks 2/4 are also independent.

**Tech Stack:** Python 3.13 (FastAPI, SQLAlchemy Core, asyncpg, alembic), Postgres 15 (LISTEN/NOTIFY + outbox pattern), TypeScript / Next.js / React, pytest + Vitest, `uv` for all Python.

**Branch:** `feat/postgres-migration-completion` (tip `941ad4ab`). Commit on top of that tip; do not start a new branch.

**Source review report:** This plan is v2 — v1 was reviewed by a Codex/Gemini/Claude tribunal and 15 issues were surfaced. v2 incorporates every finding. Source spec: `docs/plans/2026-04-28-postgres-migration-completion.md` and `…-IMPL.md`. Tribunal-confirmed facts the plan now relies on:

- Outbox NOTIFY trigger emits `NEW.id::text` only — payloads must be fetched by `SELECT payload FROM events.outbox WHERE id = ?`.
- `events.outbox.consumed_by` is a JSONB array; consumers ack by appending their identifier.
- `xenon.order_submissions` already has `perm_id`; `trades.submission_id` is the FK that lets `_trade_to_payload` plumb it through.
- IBKR Flex CLI does not currently extract `permID` from the source XML — Task 0 adds that.
- `combo_wizard.list_unresolved_orders` hardcodes the state filter; Task 2 parameterizes it before replaying UNKNOWN rows.
- `journal_entries` has no unique constraint; the upsert needs one to be idempotent under concurrency.

---

## Task ordering and dependencies

```
Task 0 (perm_id join key) ──┬──> Task 3 (PG+Flex merge) ──> Task 5 (nightly divergence)
                            │
Task 1 (auto-import listener) — independent
Task 2 (UNKNOWN replay) — independent
Task 4 (UI source pill) — depends only on data.source field, not on perm_id correctness
```

Recommended execution order: Task 1 → Task 0 → Task 2 → Task 3 → Task 4 → Task 5. Task 1 first because it's the only silent regression. Task 0 next because Tasks 3 and 5 depend on it. Tasks 2 and 4 can slot in anywhere.

---

## File Structure

| Path                                                                       | Status               | Responsibility                                                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `src/xenon/db/migrations/versions/<rev>_add_journal_auto_import_unique.py` | **create**           | Partial unique index on `journal_entries(broker, account_env, broker_account, trade_id) WHERE decision = 'IB_AUTO_IMPORT' AND trade_id IS NOT NULL`.                                                                                                                                                                                     |
| `src/xenon/db/queries/journal.py`                                          | modify               | Add `upsert_auto_import_entry()` using `INSERT ... ON CONFLICT DO NOTHING RETURNING`; resolve scope from `trades` row by trade_id (not from payload). Make `list_journal_entries` accept `cutoff: datetime                                                                                                                               | None = None`. |
| `src/xenon/api/services/journal_auto_import.py`                            | **create**           | Background subscriber: NOTIFY id → fetch outbox row → upsert via `asyncio.to_thread`; backfills via `get_events_since` on start; appends consumer id to `outbox.consumed_by`.                                                                                                                                                            |
| `src/xenon/api/server.py`                                                  | modify               | Lifespan starts/stops the subscriber. Replace `from xenon.utils.subprocess_runner` references — `run_module` is at `xenon.api.subprocess`. Add `flex_divergence` to `/health` using `_resolve_scope_kwargs()`.                                                                                                                           |
| `src/xenon/api/tests/test_journal_auto_import.py`                          | **create**           | TDD: upsert idempotent under repeated calls; listener round-trips a real outbox row id; backfill on boot ack-marks outbox rows; sync-in-async safety.                                                                                                                                                                                    |
| `src/xenon/db/queries/combo_wizard.py`                                     | modify               | `list_unresolved_orders(..., states=("PENDING","WORKING","PARTIALLY_FILLED"))` — parameterize.                                                                                                                                                                                                                                           |
| `src/xenon/execution/single_leg_rehydrate.py`                              | modify               | Add public `replay_states(states, scope) -> list[dict]` that calls the same three-source reconcile path used by `rehydrate_on_boot`, bounded to caller-supplied states.                                                                                                                                                                  |
| `scripts/migrations/_2026_04_28_replay_unknown_orders.py`                  | **create**           | One-shot script: `replay_unknown(scope) -> {"resolved": int, "still_unknown": int, "scanned": int, "errors": list[str]}`. Surfaces real failures rather than swallowing them.                                                                                                                                                            |
| `scripts/tests/test_replay_unknown_orders.py`                              | **create**           | TDD: empty case, idempotency, **resolution test using injected IB client** (asserts UNKNOWN → COMPLETED transition), failure surfacing.                                                                                                                                                                                                  |
| `src/xenon/db/queries/blotter.py`                                          | modify               | Add `perm_id` to `_trade_to_payload` via order_submissions join. Add public `compare_blotter_rows(pg_row, flex_row) -> list[str]` (returns differing field names). Add `merge_pg_and_flex(pg_payload, flex_payload) -> dict` that joins on `perm_id`, **recomputes summary**, and tags each row with `divergence` + `divergence_fields`. |
| `src/xenon/trade_blotter/flex_query.py`                                    | modify               | Extract `permID` (or `ibOrderID` fallback) from each Flex trade row and surface as `perm_id` in the JSON output.                                                                                                                                                                                                                         |
| `src/xenon/api/server.py` (blotter handlers)                               | modify               | `blotter_sync` calls `merge_pg_and_flex` when both sources have rows; falls back cleanly when one is empty.                                                                                                                                                                                                                              |
| `scripts/tests/test_blotter_merge.py`                                      | **create**           | TDD: pg-only, flex-only, both-match (no divergence), both-divergent (flagged), summary correctly recomputed, missing perm_id rows pass-through.                                                                                                                                                                                          |
| `scripts/tests/test_blotter_pg_perm_id.py`                                 | **create**           | TDD: `_trade_to_payload` includes `perm_id` when the `submission_id` resolves; null `submission_id` → `perm_id` is None.                                                                                                                                                                                                                 |
| `scripts/tests/test_flex_query_perm_id.py`                                 | **create**           | TDD: Flex CLI JSON output includes `perm_id` per trade.                                                                                                                                                                                                                                                                                  |
| `web/components/SourcePill.tsx`                                            | **create**           | Render PG / FLEX / PG+FLEX.                                                                                                                                                                                                                                                                                                              |
| `web/components/WorkspaceSections.tsx`                                     | modify               | Render `<SourcePill source={data?.source} />` in HistoricalTradesSection header.                                                                                                                                                                                                                                                         |
| `web/tests/historical-trades-source-pill.test.tsx`                         | **create**           | Vitest: pill renders for each `BlotterSource` value.                                                                                                                                                                                                                                                                                     |
| `src/xenon/db/schema.py`                                                   | modify               | Add `flex_divergence_runs` Table on `xenon_metadata` (NOT bare `metadata`).                                                                                                                                                                                                                                                              |
| `src/xenon/db/migrations/versions/<rev>_add_flex_divergence_runs.py`       | **create**           | Alembic for the new table.                                                                                                                                                                                                                                                                                                               |
| `src/xenon/jobs/__init__.py`                                               | **create if absent** | Package marker.                                                                                                                                                                                                                                                                                                                          |
| `src/xenon/jobs/flex_divergence_check.py`                                  | **create**           | CLI + library: filter PG by NYSE-session calendar boundary; reuse `compare_blotter_rows`; `record_run` / `latest_run` keyed by scope; `_main` resolves scope from app.state when invoked in-process, else env.                                                                                                                           |
| `src/xenon/api/server.py` (`/health`)                                      | modify               | `flex_divergence` field built via `_resolve_scope_kwargs()`.                                                                                                                                                                                                                                                                             |
| `scripts/tests/test_flex_divergence_check.py`                              | **create**           | TDD: `compute_divergence`, `record_run`/`latest_run`, **`_main` orchestration with mocked PG + Flex**, **Flex-unconfigured no-op**, **`/health.flex_divergence` payload shape**.                                                                                                                                                         |

---

## Task 1 — Journal IB_AUTO_IMPORT listener (W4.7)

**Why first:** This is the only silent feature regression in the PR.

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_journal_auto_import_unique.py`
- Modify: `src/xenon/db/queries/journal.py`
- Create: `src/xenon/api/services/journal_auto_import.py`
- Modify: `src/xenon/api/server.py` (lifespan around line 200)
- Test: `src/xenon/api/tests/test_journal_auto_import.py`

### Steps

- [ ] **Step 1.1: Generate alembic migration for unique constraint**

```bash
uv run alembic revision -m "add journal auto import unique index"
```

Open the generated file under `src/xenon/db/migrations/versions/` and replace its `upgrade`/`downgrade` bodies:

```python
def upgrade() -> None:
    op.create_index(
        "uq_journal_auto_import",
        "journal_entries",
        ["broker", "account_env", "broker_account", "trade_id"],
        unique=True,
        schema="xenon",
        postgresql_where=sa.text("decision = 'IB_AUTO_IMPORT' AND trade_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_journal_auto_import", table_name="journal_entries", schema="xenon")
```

Apply and verify:

```bash
uv run alembic upgrade head
uv run alembic check
```

Expected: `No new upgrade operations detected.`

- [ ] **Step 1.2: Write the failing tests for `upsert_auto_import_entry`**

Create `src/xenon/api/tests/test_journal_auto_import.py`:

```python
"""Tests for IB_AUTO_IMPORT journal listener (W4.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.events import emit_outbox_in_txn, CHANNEL_TRADE_CLOSED
from xenon.db.queries.journal import (
    list_journal_entries,
    upsert_auto_import_entry,
)
from xenon.db.schema import outbox, trades
from xenon.execution.account_scope import AccountScope


_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU111111")


def _insert_closed_trade(conn, *, ticker: str = "AAPL") -> int:
    result = conn.execute(
        insert(trades).values(
            ticker=ticker,
            action="BUY",
            entry_cost=100,
            exit_cost=120,
            realized_pnl=20,
            quantity=1,
            opened_at=datetime(2026, 4, 28, 14, tzinfo=timezone.utc),
            closed_at=datetime(2026, 4, 28, 15, tzinfo=timezone.utc),
            state="CLOSED",
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        ).returning(trades.c.id)
    )
    return int(result.scalar_one())


def test_upsert_auto_import_creates_entry_once():
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn)
        first = upsert_auto_import_entry(conn, trade_id=trade_id)
        second = upsert_auto_import_entry(conn, trade_id=trade_id)

    assert first is not None
    assert first["id"] is not None
    assert first["id"] == second["id"], "second call must return same row, not insert"
    assert first["decision"] == "IB_AUTO_IMPORT"

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE, cutoff=cutoff, limit=10)
    auto_imports = [r for r in rows if r["trade_id"] == trade_id]
    assert len(auto_imports) == 1


def test_upsert_auto_import_skips_unknown_trade():
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = upsert_auto_import_entry(conn, trade_id=999_999_999)
    assert result is None


def test_upsert_resolves_scope_from_trade_row_not_from_caller():
    """Listener does not know scope ahead of time — must read it from trades."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="MSFT")
        result = upsert_auto_import_entry(conn, trade_id=trade_id)
    assert result is not None
    assert result["broker"] == _SCOPE.broker
    assert result["account_env"] == _SCOPE.account_env
    assert result["broker_account"] == _SCOPE.broker_account
```

Run: `uv run pytest src/xenon/api/tests/test_journal_auto_import.py::test_upsert_auto_import_creates_entry_once -v`
Expected: FAIL — `upsert_auto_import_entry` not yet defined.

- [ ] **Step 1.3: Make `list_journal_entries` accept `cutoff=None`**

In `src/xenon/db/queries/journal.py`, find the existing `list_journal_entries` and change its `cutoff` parameter type to `datetime | None`. Inside the function, only add the `journal_entries.c.authored_at >= cutoff` clause when `cutoff is not None`.

Concretely, the WHERE-clause assembly becomes:

```python
conditions = [
    journal_entries.c.broker == scope.broker,
    journal_entries.c.account_env == scope.account_env,
    journal_entries.c.broker_account == scope.broker_account,
]
if cutoff is not None:
    conditions.append(journal_entries.c.authored_at >= cutoff)
```

- [ ] **Step 1.4: Implement `upsert_auto_import_entry`**

Append to `src/xenon/db/queries/journal.py`:

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
from xenon.db.schema import journal_entries, trades


def upsert_auto_import_entry(
    conn: Connection,
    *,
    trade_id: int,
) -> dict[str, Any] | None:
    """Idempotently create an IB_AUTO_IMPORT journal entry for a closed trade.

    Resolves scope from the `trades` row (not from a caller-supplied scope) so
    the listener does not need scope-bearing payloads. Relies on the partial
    unique index `uq_journal_auto_import` for concurrent safety.

    Returns the row payload, or None if the trade does not exist.
    """
    trade_row = conn.execute(
        select(
            trades.c.ticker,
            trades.c.broker,
            trades.c.account_env,
            trades.c.broker_account,
        ).where(trades.c.id == trade_id)
    ).first()
    if trade_row is None:
        return None

    stmt = (
        pg_insert(journal_entries)
        .values(
            trade_id=trade_id,
            ticker=trade_row.ticker,
            decision="IB_AUTO_IMPORT",
            authored_by="system",
            metadata={"source": "trade_closed_outbox"},
            broker=trade_row.broker,
            account_env=trade_row.account_env,
            broker_account=trade_row.broker_account,
        )
        .on_conflict_do_nothing(index_elements=["broker", "account_env", "broker_account", "trade_id"])
        .returning(journal_entries)
    )
    inserted = conn.execute(stmt).first()
    if inserted is not None:
        return journal_entry_to_payload(inserted)

    # Conflict — fetch existing.
    existing = conn.execute(
        select(journal_entries).where(
            journal_entries.c.trade_id == trade_id,
            journal_entries.c.decision == "IB_AUTO_IMPORT",
            journal_entries.c.broker == trade_row.broker,
            journal_entries.c.account_env == trade_row.account_env,
            journal_entries.c.broker_account == trade_row.broker_account,
        ).limit(1)
    ).first()
    return journal_entry_to_payload(existing) if existing is not None else None
```

If `select` and the schema imports aren't already at the top of the file, add them.

- [ ] **Step 1.5: Run the upsert tests, expect all 3 PASS**

Run: `uv run pytest src/xenon/api/tests/test_journal_auto_import.py -v -k upsert`
Expected: 3 passed.

- [ ] **Step 1.6: Write the failing test for the NOTIFY-id-based listener**

Append to the same test file:

```python
import asyncio
import json

from xenon.api.services.journal_auto_import import JournalAutoImportSubscriber


def test_listener_handles_notify_id_payload(monkeypatch):
    """NOTIFY trigger emits NEW.id::text — the listener must fetch the outbox row."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="GOOG")
        outbox_id = emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "GOOG"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.handle_notification_id(outbox_id)

    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE)
    autos = [r for r in rows if r["trade_id"] == trade_id and r["decision"] == "IB_AUTO_IMPORT"]
    assert len(autos) == 1


def test_listener_acks_consumed_by_after_processing():
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="NVDA")
        outbox_id = emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "NVDA"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.handle_notification_id(outbox_id)

    with engine.connect() as conn:
        row = conn.execute(
            select(outbox.c.consumed_by).where(outbox.c.id == outbox_id)
        ).first()
    assert "journal_auto_import" in (row.consumed_by or [])


def test_listener_backfills_unconsumed_events_on_start():
    """Events emitted before the listener boots must still create journal entries."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="META")
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "META"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.replay_unconsumed()  # synchronous backfill, called from start()

    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE)
    autos = [r for r in rows if r["trade_id"] == trade_id and r["decision"] == "IB_AUTO_IMPORT"]
    assert len(autos) == 1
```

Run: expect 3 failures (subscriber not implemented).

- [ ] **Step 1.7: Implement the listener**

Create `src/xenon/api/services/journal_auto_import.py`:

```python
"""Background subscriber: trade.closed → journal_entries(IB_AUTO_IMPORT).

Replaces the legacy periodic journal sync with a PG-event-driven pipeline.

NOTIFY contract: the outbox trigger emits NEW.id::text — payloads are not on
the wire. The listener fetches outbox.payload by id, upserts the journal row,
then appends its consumer id to outbox.consumed_by.

DB work is run synchronously inside `asyncio.to_thread` so it doesn't block
the asyncpg event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import select, update

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_TRADE_CLOSED, EventSubscriber
from xenon.db.queries.journal import upsert_auto_import_entry
from xenon.db.schema import outbox

logger = logging.getLogger(__name__)

CONSUMER_ID = "journal_auto_import"


class JournalAutoImportSubscriber:
    """Fetch-by-id listener for trade.closed outbox rows."""

    def __init__(self) -> None:
        self._subscriber: EventSubscriber | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- sync core (test-friendly, single-row) ------------------------

    def handle_notification_id(self, outbox_id: int) -> None:
        engine = get_sync_engine()
        with engine.begin() as conn:
            row = conn.execute(
                select(outbox.c.payload, outbox.c.consumed_by).where(outbox.c.id == outbox_id)
            ).first()
            if row is None:
                logger.warning("trade.closed id=%s not found in outbox", outbox_id)
                return
            consumed = list(row.consumed_by or [])
            if CONSUMER_ID in consumed:
                return  # already processed
            payload = row.payload or {}
            trade_id = payload.get("trade_id")
            if trade_id is None:
                logger.warning("trade.closed id=%s payload missing trade_id", outbox_id)
                return
            try:
                upsert_auto_import_entry(conn, trade_id=int(trade_id))
            except Exception:
                logger.exception("auto-import upsert failed for outbox id=%s", outbox_id)
                raise
            consumed.append(CONSUMER_ID)
            conn.execute(
                update(outbox).where(outbox.c.id == outbox_id).values(consumed_by=consumed)
            )

    def replay_unconsumed(self) -> int:
        """Process every trade.closed outbox row that this consumer hasn't acked."""
        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(outbox.c.id).where(outbox.c.channel == CHANNEL_TRADE_CLOSED).order_by(outbox.c.id)
            ).fetchall()
        replayed = 0
        for row in rows:
            try:
                self.handle_notification_id(int(row.id))
                replayed += 1
            except Exception:
                logger.exception("replay failed for outbox id=%s", row.id)
        return replayed

    # ---- async wiring -------------------------------------------------

    async def start(self) -> None:
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            logger.warning("DATABASE_URL not set; journal auto-import listener disabled")
            return
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.replay_unconsumed)
        self._subscriber = EventSubscriber(dsn=dsn, channels=[CHANNEL_TRADE_CLOSED])
        self._subscriber.on(CHANNEL_TRADE_CLOSED, self._on_notification)
        await self._subscriber.start()
        logger.info("journal auto-import listener started")

    def _on_notification(self, _channel: str, payload: str) -> None:
        try:
            outbox_id = int(payload)
        except (TypeError, ValueError):
            logger.warning("trade.closed NOTIFY payload not int: %r", payload)
            return
        loop = self._loop
        if loop is None:
            logger.error("listener received notification before loop was set")
            return
        loop.create_task(asyncio.to_thread(self.handle_notification_id, outbox_id))

    async def stop(self) -> None:
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None
```

Run all 6 tests in the file: `uv run pytest src/xenon/api/tests/test_journal_auto_import.py -v`
Expected: 6 passed.

- [ ] **Step 1.8: Wire the subscriber into the FastAPI lifespan**

In `src/xenon/api/server.py`, in the `lifespan` async context manager startup section (after the existing combo-wizard rehydrate block, before `yield`):

```python
from xenon.api.services.journal_auto_import import JournalAutoImportSubscriber

journal_auto_import = JournalAutoImportSubscriber()
try:
    await journal_auto_import.start()
    app.state.journal_auto_import = journal_auto_import
except Exception:  # noqa: BLE001
    logger.exception("journal auto-import listener failed to start")
```

In the shutdown section (after `yield`):

```python
listener = getattr(app.state, "journal_auto_import", None)
if listener is not None:
    try:
        await listener.stop()
    except Exception:  # noqa: BLE001
        logger.exception("journal auto-import listener failed to stop")
```

- [ ] **Step 1.9: Regression run**

```bash
uv run pytest src/xenon/api/tests/test_journal_auto_import.py src/xenon/api/tests/test_journal_endpoint.py src/xenon/api/tests/test_journal_sync_endpoint.py -v
```

Expected: all green.

- [ ] **Step 1.10: Commit**

```bash
git add src/xenon/db/migrations/versions/*journal_auto_import*.py \
        src/xenon/db/queries/journal.py \
        src/xenon/api/services/journal_auto_import.py \
        src/xenon/api/server.py \
        src/xenon/api/tests/test_journal_auto_import.py
git commit -m "feat(journal): pg listener for IB_AUTO_IMPORT entries (W4.7)"
```

---

## Task 0 — Plumb `perm_id` through both blotter payloads (prerequisite for Tasks 3 and 5)

**Why:** v1 review confirmed neither PG nor Flex blotter payloads emit `perm_id`. Without a stable join key, `merge_pg_and_flex` and the divergence job have no way to align rows. Adds the field to both producers; downstream tasks then have a real key.

**Files:**

- Modify: `src/xenon/db/queries/blotter.py` (`fetch_blotter_pg` and `_trade_to_payload`)
- Modify: `src/xenon/trade_blotter/flex_query.py`
- Test: `scripts/tests/test_blotter_pg_perm_id.py`
- Test: `scripts/tests/test_flex_query_perm_id.py`

### Steps

- [ ] **Step 0.1: Write the failing test for PG `perm_id` plumbing**

Create `scripts/tests/test_blotter_pg_perm_id.py`:

```python
"""Tests for perm_id plumbing through the PG blotter payload (Task 0)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.queries.blotter import fetch_blotter_pg
from xenon.db.schema import order_submissions, trades
from xenon.execution.account_scope import AccountScope


_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU000007")


def _insert_submission(conn, submission_id: str, perm_id: str | None) -> None:
    conn.execute(
        insert(order_submissions).values(
            submission_id=submission_id,
            ticker="AAPL",
            security_type="STK",
            action="BUY",
            quantity=1,
            limit_price=100,
            state="COMPLETED",
            perm_id=perm_id,
            submitted_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        )
    )


def _insert_trade(conn, *, submission_id: str | None, ticker: str = "AAPL") -> int:
    res = conn.execute(
        insert(trades).values(
            ticker=ticker,
            action="BUY",
            quantity=1,
            entry_cost=100,
            exit_cost=110,
            realized_pnl=10,
            opened_at=datetime(2026, 4, 27, 14, tzinfo=timezone.utc),
            closed_at=datetime(2026, 4, 27, 15, tzinfo=timezone.utc),
            state="CLOSED",
            submission_id=submission_id,
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        ).returning(trades.c.id)
    )
    return int(res.scalar_one())


def test_payload_includes_perm_id_when_submission_resolves():
    engine = get_sync_engine()
    with engine.begin() as conn:
        _insert_submission(conn, "sub-permid-001", perm_id="PERM-1")
        _insert_trade(conn, submission_id="sub-permid-001")

    with engine.connect() as conn:
        payload = fetch_blotter_pg(conn, scope=_SCOPE, days=30)

    closed = payload["closed_trades"]
    assert any(t.get("perm_id") == "PERM-1" for t in closed)


def test_payload_perm_id_none_when_submission_missing():
    engine = get_sync_engine()
    with engine.begin() as conn:
        _insert_trade(conn, submission_id=None, ticker="MSFT")

    with engine.connect() as conn:
        payload = fetch_blotter_pg(conn, scope=_SCOPE, days=30)

    msft_rows = [t for t in payload["closed_trades"] if t["symbol"] == "MSFT"]
    assert msft_rows
    assert all(t.get("perm_id") is None for t in msft_rows)
```

Run: `uv run pytest scripts/tests/test_blotter_pg_perm_id.py -v`
Expected: FAIL — payloads don't include `perm_id`.

- [ ] **Step 0.2: Plumb `perm_id` in `fetch_blotter_pg`**

In `src/xenon/db/queries/blotter.py`, change `fetch_blotter_pg` to LEFT JOIN `order_submissions` and pass `perm_id` into `_trade_to_payload`:

```python
from xenon.db.schema import order_submissions, trades  # ensure import


def fetch_blotter_pg(
    conn: Connection,
    *,
    scope: AccountScope,
    days: int = 30,
) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trade_time = func.coalesce(trades.c.closed_at, trades.c.opened_at)
    rows = conn.execute(
        select(trades, order_submissions.c.perm_id.label("perm_id"))
        .select_from(
            trades.outerjoin(
                order_submissions,
                trades.c.submission_id == order_submissions.c.submission_id,
            )
        )
        .where(
            trades.c.broker == scope.broker,
            trades.c.account_env == scope.account_env,
            trades.c.broker_account == scope.broker_account,
            or_(trade_time >= cutoff, trade_time.is_(None)),
        )
        .order_by(desc(trade_time), desc(trades.c.id))
    ).all()
    # ...rest unchanged, except _trade_to_payload now sees row._mapping["perm_id"]
```

In `_trade_to_payload`, add to the returned dict:

```python
        "perm_id": row.get("perm_id"),
```

Run the test, expect both pass.

- [ ] **Step 0.3: Write the failing test for Flex `perm_id`**

Create `scripts/tests/test_flex_query_perm_id.py`:

```python
"""Flex CLI must surface perm_id (Task 0)."""

from __future__ import annotations

import json

from xenon.trade_blotter.flex_query import _build_payload_from_trades  # adjust if helper has a different name


def test_flex_payload_includes_perm_id():
    raw_trades = [
        {
            "tradeID": "T1",
            "ibOrderID": "ORD-1",
            "permID": "PERM-X",
            "symbol": "AAPL",
            "buySell": "BUY",
            "quantity": "1",
            "price": "100",
            "commission": "0.5",
            "dateTime": "20260427;143000",
        }
    ]
    payload = _build_payload_from_trades(raw_trades)
    closed = payload.get("closed_trades", [])
    assert closed
    assert closed[0].get("perm_id") == "PERM-X"
```

If the actual helper used by the Flex CLI to build the JSON payload has a different name, find it via `grep -n 'closed_trades\|def.*payload' src/xenon/trade_blotter/flex_query.py` and adjust the import + call accordingly. The test must call whatever public function the CLI uses to convert raw IBKR Flex trade dicts into the same `BlotterData` shape the route returns.

Run: `uv run pytest scripts/tests/test_flex_query_perm_id.py -v`
Expected: FAIL — perm_id field not present.

- [ ] **Step 0.4: Plumb `perm_id` in Flex CLI output**

In `src/xenon/trade_blotter/flex_query.py`, locate the place where each Flex trade dict is converted into the payload shape. Add an extraction:

```python
perm_id = str(trade.get("permID") or trade.get("ibOrderID") or "") or None
```

…and include it in the row dict that ends up in `closed_trades`/`open_trades` at the same level as `symbol`/`sec_type`. (The exact field name and key lives next to `tradeID` extraction at line 231 — same row, just add the new column.)

Run the test, expect pass.

- [ ] **Step 0.5: Commit**

```bash
git add src/xenon/db/queries/blotter.py \
        src/xenon/trade_blotter/flex_query.py \
        scripts/tests/test_blotter_pg_perm_id.py \
        scripts/tests/test_flex_query_perm_id.py
git commit -m "feat(blotter): plumb perm_id through PG and Flex payloads (W3.4 prereq)"
```

---

## Task 2 — Replay UNKNOWN order submissions (W1.3)

**Why:** Spec acceptance criterion: zero `state=UNKNOWN` rows older than 1 hour. v1 review confirmed `single_leg_rehydrate.rehydrate_on_boot` filters PENDING/WORKING/PARTIALLY_FILLED only; UNKNOWN rows are skipped. Plan adds a `states` parameter to `list_unresolved_orders` and a public `replay_states` entrypoint, then writes the script.

**Files:**

- Modify: `src/xenon/db/queries/combo_wizard.py` (`list_unresolved_orders` accepts `states` tuple)
- Modify: `src/xenon/execution/single_leg_rehydrate.py` (add public `replay_states`)
- Create: `scripts/migrations/_2026_04_28_replay_unknown_orders.py`
- Test: `scripts/tests/test_replay_unknown_orders.py`

### Steps

- [ ] **Step 2.1: Make the state filter parameterizable**

In `src/xenon/db/queries/combo_wizard.py`, change `list_unresolved_orders`:

```python
DEFAULT_UNRESOLVED_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def list_unresolved_orders(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
    states: tuple[str, ...] = DEFAULT_UNRESOLVED_STATES,
) -> list[dict]:
    from xenon.db.schema import order_submissions

    conditions = [order_submissions.c.state.in_(states)]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    result = conn.execute(select(order_submissions).where(*conditions))
    return [dict(r._mapping) for r in result]
```

- [ ] **Step 2.2: Add public `replay_states` to `single_leg_rehydrate`**

In `src/xenon/execution/single_leg_rehydrate.py`, just before `rehydrate_on_boot()`:

```python
def replay_states(
    states: tuple[str, ...],
    *,
    scope: AccountScope,
    ib_client_factory=None,
) -> list[dict]:
    """Run the three-source reconcile against rows in the given states.

    Mirrors `rehydrate_on_boot`'s flow but lets callers narrow the state
    filter (e.g. to ['UNKNOWN']) without touching the boot-time path.

    Returns a list of {submission_id, before_state, after_state, error?} dicts.
    """
    rows = combo_wizard.list_unresolved_orders(
        get_sync_engine().connect().__enter__(),  # short-lived; replaced below
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
        states=states,
    )
    # The connection above is leaked by design of __enter__ — replace with a real with-block:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = combo_wizard.list_unresolved_orders(
            conn,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            states=states,
        )

    if not rows:
        return []

    factory = ib_client_factory or _default_ib_client_factory()  # whatever the existing helper is
    ib = factory()
    open_orders = ib.get_open_orders() or []
    execs = ib.get_executions() or []
    positions_raw = ib.get_positions() or []
    positions = _build_positions_snapshot(positions_raw, rows)

    open_idx = _index_open_orders(open_orders)
    exec_idx = _index_executions(execs)
    exec_records = _index_execution_records(execs)

    outcomes: list[dict] = []
    for row in rows:
        before = row.get("state")
        try:
            outcome = _reconcile_from_three_sources(row, open_idx, exec_idx, positions)
            after = outcome.get("state") if outcome else before
            if after and after != before:
                _update_state_only(row["submission_id"], after)
            outcomes.append({"submission_id": row["submission_id"], "before_state": before, "after_state": after})
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"submission_id": row["submission_id"], "before_state": before, "after_state": before, "error": str(exc)})
    return outcomes
```

The exact name `_default_ib_client_factory` may differ in this file — the existing `rehydrate_on_boot` already uses an IB factory. Reuse the same one. Trim the duplicated `with engine.connect()` block I left for clarity; only the second one is needed.

- [ ] **Step 2.3: Write the failing tests for the replay script**

Create `scripts/tests/test_replay_unknown_orders.py`:

```python
"""Tests for the UNKNOWN order replay backfill (W1.3)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select

from scripts.migrations._2026_04_28_replay_unknown_orders import replay_unknown
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope


_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU222222")


def _reset_unknown_for_scope() -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            order_submissions.delete().where(
                order_submissions.c.broker_account == _SCOPE.broker_account
            )
        )


def _insert_unknown(submission_id: str, *, ib_order_id: str | None = None) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                ticker="SPY",
                security_type="STK",
                action="BUY",
                quantity=10,
                limit_price=400,
                state="UNKNOWN",
                ib_order_id=ib_order_id,
                submitted_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
                broker=_SCOPE.broker,
                account_env=_SCOPE.account_env,
                broker_account=_SCOPE.broker_account,
            )
        )


def _current_state(submission_id: str) -> str:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(
                order_submissions.c.submission_id == submission_id
            )
        ).scalar_one()


def test_replay_no_unknown_rows_is_noop():
    _reset_unknown_for_scope()
    summary = replay_unknown(scope=_SCOPE, ib_client_factory=lambda: SimpleNamespace(get_open_orders=lambda: [], get_executions=lambda: [], get_positions=lambda: []))
    assert summary == {"resolved": 0, "still_unknown": 0, "scanned": 0, "errors": []}


def test_replay_resolves_via_executions():
    """When IB shows the order completed, replay must transition UNKNOWN → COMPLETED."""
    _reset_unknown_for_scope()
    _insert_unknown("sub-replay-A", ib_order_id="42")

    fake_ib = SimpleNamespace(
        get_open_orders=lambda: [],
        get_executions=lambda: [
            {"orderId": 42, "permId": "P1", "symbol": "SPY", "side": "BUY", "shares": 10, "price": 400, "time": "2026-04-27T15:00:00Z"}
        ],
        get_positions=lambda: [],
    )
    summary = replay_unknown(scope=_SCOPE, ib_client_factory=lambda: fake_ib)
    assert summary["scanned"] == 1
    assert _current_state("sub-replay-A") != "UNKNOWN", \
        f"expected state to transition away from UNKNOWN, got summary={summary}"


def test_replay_surfaces_errors_rather_than_silencing_them():
    _reset_unknown_for_scope()
    _insert_unknown("sub-replay-B")

    def boom():
        raise RuntimeError("ib unreachable")

    fake_ib = SimpleNamespace(get_open_orders=boom, get_executions=lambda: [], get_positions=lambda: [])
    summary = replay_unknown(scope=_SCOPE, ib_client_factory=lambda: fake_ib)
    assert summary["scanned"] == 1
    assert summary["errors"], "errors must be surfaced, not silenced"
```

Run: `uv run pytest scripts/tests/test_replay_unknown_orders.py -v`
Expected: 3 fail (script not implemented).

- [ ] **Step 2.4: Implement the replay script**

Create `scripts/migrations/_2026_04_28_replay_unknown_orders.py`:

```python
"""One-shot replay: resolve pre-existing UNKNOWN order_submissions rows.

Spec: docs/plans/2026-04-28-postgres-migration-completion-IMPL.md §W1.3.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Callable

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope, resolve_from_env
from xenon.execution.single_leg_rehydrate import replay_states

logger = logging.getLogger(__name__)


def replay_unknown(*, scope: AccountScope, ib_client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    outcomes = replay_states(("UNKNOWN",), scope=scope, ib_client_factory=ib_client_factory)
    scanned = len(outcomes)
    resolved = sum(1 for o in outcomes if o.get("after_state") and o["after_state"] != "UNKNOWN" and "error" not in o)
    still_unknown = scanned - resolved
    errors = [f"{o['submission_id']}: {o['error']}" for o in outcomes if "error" in o]
    return {"resolved": resolved, "still_unknown": still_unknown, "scanned": scanned, "errors": errors}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay UNKNOWN order_submissions through rehydrate.")
    parser.add_argument("--apply", action="store_true", help="Execute replay (default is dry-run summary).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    scope = resolve_from_env()
    if not args.apply:
        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(order_submissions.c.submission_id).where(
                    order_submissions.c.state == "UNKNOWN",
                    order_submissions.c.broker == scope.broker,
                    order_submissions.c.account_env == scope.account_env,
                    order_submissions.c.broker_account == scope.broker_account,
                )
            ).fetchall()
        print(json.dumps({"dry_run": True, "unknown_count": len(rows), "scope": scope.as_dict()}, indent=2))
        return 0

    summary = replay_unknown(scope=scope)
    print(json.dumps({"applied": True, "scope": scope.as_dict(), **summary}, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(_main())
```

Run the tests, expect all 3 pass.

- [ ] **Step 2.5: Smoke-run dry mode**

```bash
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=$IB_PAPER_ACCOUNT uv run python -m scripts.migrations._2026_04_28_replay_unknown_orders
```

Expected: prints the current UNKNOWN count.

- [ ] **Step 2.6: Commit**

```bash
git add src/xenon/db/queries/combo_wizard.py \
        src/xenon/execution/single_leg_rehydrate.py \
        scripts/migrations/_2026_04_28_replay_unknown_orders.py \
        scripts/tests/test_replay_unknown_orders.py
git commit -m "feat(migration): replay UNKNOWN order_submissions (W1.3)"
```

---

## Task 3 — PG + Flex blotter overlay merge (W3.4)

**Files:**

- Modify: `src/xenon/db/queries/blotter.py` (add `compare_blotter_rows` public, `merge_pg_and_flex` recomputes summary)
- Modify: `src/xenon/api/server.py` (`blotter_sync` calls merge)
- Test: `scripts/tests/test_blotter_merge.py`

### Steps

- [ ] **Step 3.1: Write the failing tests**

Create `scripts/tests/test_blotter_merge.py`:

```python
"""Tests for PG+Flex overlay merge (W3.4)."""

from __future__ import annotations

from xenon.db.queries.blotter import compare_blotter_rows, merge_pg_and_flex


def _trade(perm_id, *, realized_pnl=12.34, total_commission=0.5, symbol="AAPL"):
    return {
        "perm_id": perm_id,
        "symbol": symbol,
        "sec_type": "OPT",
        "is_closed": True,
        "net_quantity": 0,
        "total_quantity": 1,
        "total_commission": total_commission,
        "realized_pnl": realized_pnl,
        "cost_basis": 100,
        "proceeds": 110,
        "executions": [{"time": "2026-04-28T14:30:00Z"}],
    }


def test_compare_blotter_rows_returns_differing_field_names():
    pg = _trade("p1", realized_pnl=12.34)
    flex_match = _trade("p1", realized_pnl=12.34)
    flex_diff = _trade("p1", realized_pnl=99.99)
    assert compare_blotter_rows(pg, flex_match) == []
    assert compare_blotter_rows(pg, flex_diff) == ["realized_pnl"]


def test_merge_pg_only_when_flex_empty():
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": "2026-04-28T16:00:00Z",
        "closed_trades": [_trade("p1")],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 12.34},
    }
    flex = {"closed_trades": [], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["source"] == "postgres"
    assert merged["closed_trades"][0]["divergence"] is False


def test_merge_disjoint_perm_ids_recomputes_summary():
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": "2026-04-28T16:00:00Z",
        "closed_trades": [_trade("p1", realized_pnl=10, total_commission=1)],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 1, "realized_pnl": 10},
    }
    flex = {"closed_trades": [_trade("p2", realized_pnl=20, total_commission=2)], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["source"] == "postgres+flex"
    assert merged["summary"]["closed_trades"] == 2
    assert abs(merged["summary"]["total_commissions"] - 3) < 1e-6
    assert abs(merged["summary"]["realized_pnl"] - 30) < 1e-6


def test_merge_flags_divergent_realized_pnl():
    pg = {"configured": True, "source": "postgres", "as_of": "2026-04-28T16:00:00Z",
          "closed_trades": [_trade("p1", realized_pnl=10)], "open_trades": [],
          "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 10}}
    flex = {"closed_trades": [_trade("p1", realized_pnl=20)], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    [row] = merged["closed_trades"]
    assert row["divergence"] is True
    assert row["divergence_fields"] == ["realized_pnl"]


def test_rows_without_perm_id_pass_through_pg_side():
    pg = {"configured": True, "source": "postgres", "as_of": None,
          "closed_trades": [{**_trade(None), "perm_id": None}], "open_trades": [],
          "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 12.34}}
    flex = {"closed_trades": [], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["closed_trades"][0]["divergence"] is False
```

Run: expect 5 fail (functions don't exist yet).

- [ ] **Step 3.2: Implement the merge**

Append to `src/xenon/db/queries/blotter.py`:

```python
from decimal import Decimal


_DIVERGENCE_TOLERANCE = 0.01
_DIVERGENCE_FIELDS = ("realized_pnl", "total_quantity", "total_commission", "cost_basis", "proceeds")


def compare_blotter_rows(pg_row: dict, flex_row: dict) -> list[str]:
    """Return list of fields that differ between PG and Flex by more than tolerance."""
    differing: list[str] = []
    for field in _DIVERGENCE_FIELDS:
        pg_val = pg_row.get(field)
        flex_val = flex_row.get(field)
        if pg_val is None or flex_val is None:
            continue
        try:
            if abs(float(pg_val) - float(flex_val)) > _DIVERGENCE_TOLERANCE:
                differing.append(field)
        except (TypeError, ValueError):
            if pg_val != flex_val:
                differing.append(field)
    return differing


def _trade_index(rows: list[dict]) -> dict[str, dict]:
    return {r["perm_id"]: r for r in rows if r.get("perm_id")}


def _merge_section(pg_rows: list[dict], flex_rows: list[dict]) -> tuple[list[dict], bool]:
    pg_index = _trade_index(pg_rows)
    flex_index = _trade_index(flex_rows)
    overlap = set(pg_index) & set(flex_index)
    has_overlap = bool(overlap)
    merged: list[dict] = []
    for perm_id in sorted(set(pg_index) | set(flex_index)):
        pg_row = pg_index.get(perm_id)
        flex_row = flex_index.get(perm_id)
        if pg_row and flex_row:
            differing = compare_blotter_rows(pg_row, flex_row)
            merged.append({**pg_row, "divergence": bool(differing), "divergence_fields": differing})
        elif pg_row:
            merged.append({**pg_row, "divergence": False})
        else:
            merged.append({**flex_row, "divergence": False})
    merged.extend({**r, "divergence": False} for r in pg_rows if not r.get("perm_id"))
    return merged, has_overlap


def _summary_from_rows(closed: list[dict], open_: list[dict]) -> dict[str, Any]:
    total_commission = Decimal("0")
    realized_pnl = Decimal("0")
    for row in closed + open_:
        if row.get("total_commission") is not None:
            total_commission += Decimal(str(row["total_commission"]))
    for row in closed:
        if row.get("realized_pnl") is not None:
            realized_pnl += Decimal(str(row["realized_pnl"]))
    return {
        "closed_trades": len(closed),
        "open_trades": len(open_),
        "total_commissions": float(total_commission),
        "realized_pnl": float(realized_pnl),
    }


def merge_pg_and_flex(pg_payload: dict, flex_payload: dict) -> dict:
    pg_closed = list(pg_payload.get("closed_trades") or [])
    pg_open = list(pg_payload.get("open_trades") or [])
    flex_closed = list(flex_payload.get("closed_trades") or [])
    flex_open = list(flex_payload.get("open_trades") or [])

    closed, _ = _merge_section(pg_closed, flex_closed)
    open_, _ = _merge_section(pg_open, flex_open)

    flex_contributed = bool(flex_closed or flex_open)
    pg_contributed = bool(pg_closed or pg_open)
    if flex_contributed and pg_contributed:
        source = "postgres+flex"
    elif pg_contributed:
        source = "postgres"
    elif flex_contributed:
        source = "flex"
    else:
        source = "none"

    return {
        **pg_payload,
        "source": source,
        "closed_trades": closed,
        "open_trades": open_,
        "summary": _summary_from_rows(closed, open_),
    }
```

Run all blotter tests: `uv run pytest scripts/tests/test_blotter_merge.py scripts/tests/test_blotter_query.py -v`
Expected: all green.

- [ ] **Step 3.3: Wire merge into the `/blotter` POST handler**

In `src/xenon/api/server.py`, replace the body of `blotter_sync` (currently around lines 2994–3026) with:

```python
    engine = get_sync_engine()
    with engine.connect() as conn:
        pg_payload = fetch_blotter_pg(conn, scope=scope, days=30)

    pg_has = blotter_has_trades(pg_payload)
    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)

    if not result.ok:
        is_unconfigured = result.exit_code == 2 or (result.error and "FLEX_NOT_CONFIGURED" in result.error)
        if is_unconfigured:
            if pg_has:
                return pg_payload
            return {
                "configured": False,
                "as_of": None,
                "summary": {"closed_trades": 0, "open_trades": 0, "total_commissions": 0, "realized_pnl": 0},
                "closed_trades": [],
                "open_trades": [],
                "source": "none",
                "message": (
                    "IB Flex Query not configured. Set IB_FLEX_TOKEN and "
                    "IB_FLEX_QUERY_ID in .env, then click Refresh. Run "
                    "`uv run python -m xenon.trade_blotter.flex_query --setup` "
                    "for the configuration guide."
                ),
            }
        if pg_has:
            return pg_payload
        raise HTTPException(status_code=502, detail=result.error)

    flex_payload = {**result.data, "configured": True}
    merged = merge_pg_and_flex(pg_payload, flex_payload)
    merged["configured"] = True
    return merged
```

Update the import line near the top:

```python
from xenon.db.queries.blotter import blotter_has_trades, fetch_blotter_pg, merge_pg_and_flex
```

- [ ] **Step 3.4: Regression test on the route layer**

```bash
uv run pytest scripts/tests/test_blotter_merge.py scripts/tests/test_blotter_query.py scripts/tests/test_blotter_unconfigured.py -v
cd web && npx vitest run web/tests/blotter-route-pg.test.ts
```

- [ ] **Step 3.5: Commit**

```bash
git add src/xenon/db/queries/blotter.py src/xenon/api/server.py scripts/tests/test_blotter_merge.py
git commit -m "feat(blotter): merge PG and Flex with divergence flag and summary recompute (W3.4)"
```

---

## Task 4 — Source pill in HistoricalTradesSection (W3.6)

**Files:**

- Create: `web/components/SourcePill.tsx`
- Modify: `web/components/WorkspaceSections.tsx` (`HistoricalTradesSection` header, around line 3482)
- Test: `web/tests/historical-trades-source-pill.test.tsx`

### Steps

- [ ] **Step 4.1: Failing test**

Create `web/tests/historical-trades-source-pill.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { SourcePill } from "../components/SourcePill";

describe("SourcePill", () => {
  it("renders nothing when source is missing or 'none'", () => {
    const { container } = render(<SourcePill source={undefined} />);
    expect(container.firstChild).toBeNull();
    const { container: c2 } = render(<SourcePill source="none" />);
    expect(c2.firstChild).toBeNull();
  });

  it.each([
    ["postgres", "PG"],
    ["flex", "FLEX"],
    ["postgres+flex", "PG+FLEX"],
  ] as const)("renders the %s pill", (source, label) => {
    render(<SourcePill source={source} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});
```

Run: `cd web && npx vitest run web/tests/historical-trades-source-pill.test.tsx` — expect FAIL.

- [ ] **Step 4.2: Implement the component**

Create `web/components/SourcePill.tsx`:

```tsx
import type { BlotterSource } from "../lib/types";

const LABELS: Record<Exclude<BlotterSource, "none">, string> = {
  postgres: "PG",
  flex: "FLEX",
  "postgres+flex": "PG+FLEX",
};

export function SourcePill({ source }: { source?: BlotterSource }) {
  if (!source || source === "none") return null;
  return (
    <span className="pill neutral" data-testid="source-pill">
      {LABELS[source]}
    </span>
  );
}
```

Run the test, expect 4 passed.

- [ ] **Step 4.3: Render the pill**

In `web/components/WorkspaceSections.tsx`, find the section header inside `HistoricalTradesSection` (around line 3482). Just after the `as_of` `<span>` and before the `TableSearch`, add:

```tsx
<SourcePill source={data?.source} />
```

Add the import near the top of the file (group with other component imports):

```tsx
import { SourcePill } from "./SourcePill";
```

- [ ] **Step 4.4: E2E visual verification (per `web/CLAUDE.md`)**

```bash
cd web && npm run dev
```

Open http://localhost:3000, scroll to "Historical Trades (30 Days)" header. The pill should display PG, FLEX, or PG+FLEX based on `data.source`. Capture screenshot via chrome-cdp/Playwright; attach to the PR.

- [ ] **Step 4.5: Commit**

```bash
git add web/components/SourcePill.tsx \
        web/components/WorkspaceSections.tsx \
        web/tests/historical-trades-source-pill.test.tsx
git commit -m "feat(blotter): show PG/FLEX/PG+FLEX source pill (W3.6)"
```

---

## Task 5 — Nightly PG↔Flex divergence on `/health` (V.4)

**Files:**

- Modify: `src/xenon/db/schema.py` (add `flex_divergence_runs` Table on `xenon_metadata`)
- Create: `src/xenon/db/migrations/versions/<rev>_add_flex_divergence_runs.py`
- Create: `src/xenon/jobs/__init__.py` (if absent), `src/xenon/jobs/flex_divergence_check.py`
- Modify: `src/xenon/api/server.py` (`_health` adds `flex_divergence` via `_resolve_scope_kwargs`)
- Test: `scripts/tests/test_flex_divergence_check.py`

### Steps

- [ ] **Step 5.1: Add the table to `schema.py`**

Append to `src/xenon/db/schema.py` (after the `journal_entries` table definition; reuse imports already present at the top of the file — `BigInteger`, `Integer`, `Text`, `TIMESTAMP`, `JSONB`, `text`):

```python
flex_divergence_runs = Table(
    "flex_divergence_runs",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ran_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    Column("scope_broker", Text, nullable=False),
    Column("scope_account_env", Text, nullable=False),
    Column("scope_broker_account", Text, nullable=False),
    Column("total_compared", Integer, nullable=False),
    Column("divergence_count", Integer, nullable=False),
    Column("notes", JSONB, nullable=True),
)
```

(Note: `xenon_metadata`, NOT bare `metadata`. The bare name is undefined and would cause an ImportError.)

- [ ] **Step 5.2: Generate and apply alembic migration**

```bash
uv run alembic revision --autogenerate -m "add flex_divergence_runs"
```

Open the generated file, verify it only emits `op.create_table('flex_divergence_runs', ...)`. Strip any unrelated drops/renames autogenerate may pull in.

```bash
uv run alembic upgrade head
uv run alembic check
```

Expected: `No new upgrade operations detected.`

- [ ] **Step 5.3: Failing tests**

Create `scripts/tests/test_flex_divergence_check.py`:

```python
"""Tests for the nightly PG↔Flex divergence job (V.4)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from xenon.execution.account_scope import AccountScope
from xenon.jobs.flex_divergence_check import (
    compute_divergence,
    record_run,
    latest_run,
    _main,
)


_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU333333")


def test_compute_divergence_counts_disagreements():
    pg = {"closed_trades": [
        {"perm_id": "x1", "realized_pnl": 10.0, "total_commission": 1},
        {"perm_id": "x2", "realized_pnl": 5.0, "total_commission": 1},
    ]}
    flex = {"closed_trades": [
        {"perm_id": "x1", "realized_pnl": 10.0, "total_commission": 1},
        {"perm_id": "x2", "realized_pnl": 5.5, "total_commission": 1},
        {"perm_id": "x3", "realized_pnl": 1.0, "total_commission": 1},
    ]}
    summary = compute_divergence(pg, flex)
    assert summary["total_compared"] == 2
    assert summary["divergence_count"] == 1


def test_record_run_round_trips_latest():
    summary = {"total_compared": 4, "divergence_count": 1, "notes": {"sample": "ok"}}
    inserted_id = record_run(scope=_SCOPE, summary=summary)
    latest = latest_run(scope=_SCOPE)
    assert latest is not None
    assert latest["id"] == inserted_id
    assert latest["divergence_count"] == 1
    assert latest["total_compared"] == 4
    assert latest["ran_at"] is not None


def test_main_skips_when_flex_unavailable():
    fake_pg = {"closed_trades": [], "open_trades": []}

    async def fake_run_module(*_a, **_kw):
        return MagicMock(ok=False, exit_code=2, error="FLEX_NOT_CONFIGURED", data=None)

    with patch("xenon.jobs.flex_divergence_check.fetch_blotter_pg", return_value=fake_pg), \
         patch("xenon.jobs.flex_divergence_check.run_module", side_effect=fake_run_module):
        rc = _main(["--apply"])
    assert rc == 0  # graceful no-op


def test_main_records_a_run_when_both_sides_present():
    fake_pg = {"closed_trades": [{"perm_id": "p1", "realized_pnl": 10}], "open_trades": []}
    fake_flex_data = {"closed_trades": [{"perm_id": "p1", "realized_pnl": 10}], "open_trades": []}

    async def fake_run_module(*_a, **_kw):
        return MagicMock(ok=True, data=fake_flex_data)

    with patch("xenon.jobs.flex_divergence_check.fetch_blotter_pg", return_value=fake_pg), \
         patch("xenon.jobs.flex_divergence_check.run_module", side_effect=fake_run_module):
        rc = _main(["--apply"])
    assert rc == 0
    assert latest_run(scope=_SCOPE) is not None
```

Run: expect failures (module doesn't exist).

- [ ] **Step 5.4: Implement the job**

If absent, create `src/xenon/jobs/__init__.py` (empty).

Create `src/xenon/jobs/flex_divergence_check.py`:

```python
"""Nightly PG↔Flex divergence job (V.4)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from xenon.api.subprocess import run_module  # NOTE: xenon.api.subprocess, NOT xenon.utils.subprocess_runner
from xenon.db.engine import get_sync_engine
from xenon.db.queries.blotter import compare_blotter_rows, fetch_blotter_pg
from xenon.db.schema import flex_divergence_runs
from xenon.execution.account_scope import AccountScope, resolve_from_env

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")


def yesterday_session_window() -> tuple[datetime, datetime]:
    """Return [yesterday 00:00 ET, today 00:00 ET) as UTC datetimes."""
    now_ny = datetime.now(_NY)
    today_midnight_ny = datetime.combine(now_ny.date(), time(0, 0), tzinfo=_NY)
    yesterday_midnight_ny = today_midnight_ny - timedelta(days=1)
    return yesterday_midnight_ny.astimezone(timezone.utc), today_midnight_ny.astimezone(timezone.utc)


def compute_divergence(pg: dict[str, Any], flex: dict[str, Any]) -> dict[str, Any]:
    pg_rows = {r["perm_id"]: r for r in pg.get("closed_trades", []) if r.get("perm_id")}
    flex_rows = {r["perm_id"]: r for r in flex.get("closed_trades", []) if r.get("perm_id")}
    overlap = sorted(set(pg_rows) & set(flex_rows))
    diverged = [pid for pid in overlap if compare_blotter_rows(pg_rows[pid], flex_rows[pid])]
    return {
        "total_compared": len(overlap),
        "divergence_count": len(diverged),
        "notes": {"diverged_perm_ids": diverged[:50]},
    }


def record_run(*, scope: AccountScope, summary: dict[str, Any]) -> int:
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            insert(flex_divergence_runs).values(
                scope_broker=scope.broker,
                scope_account_env=scope.account_env,
                scope_broker_account=scope.broker_account,
                total_compared=int(summary["total_compared"]),
                divergence_count=int(summary["divergence_count"]),
                notes=summary.get("notes"),
            ).returning(flex_divergence_runs.c.id)
        )
        return int(result.scalar_one())


def latest_run(*, scope: AccountScope) -> dict[str, Any] | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(flex_divergence_runs).where(
                flex_divergence_runs.c.scope_broker == scope.broker,
                flex_divergence_runs.c.scope_account_env == scope.account_env,
                flex_divergence_runs.c.scope_broker_account == scope.broker_account,
            ).order_by(flex_divergence_runs.c.ran_at.desc()).limit(1)
        ).first()
    return dict(row._mapping) if row is not None else None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly PG↔Flex divergence check.")
    parser.add_argument("--apply", action="store_true", help="Insert a flex_divergence_runs row.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    scope = resolve_from_env()
    start_utc, end_utc = yesterday_session_window()
    days = max(1, int((datetime.now(timezone.utc) - start_utc).total_seconds() // 86400) + 1)
    engine = get_sync_engine()
    with engine.connect() as conn:
        pg_payload = fetch_blotter_pg(conn, scope=scope, days=days)

    pg_filtered = {
        "closed_trades": [
            r for r in pg_payload.get("closed_trades", [])
            if any(start_utc <= datetime.fromisoformat(ex["time"].replace("Z", "+00:00")) < end_utc for ex in r.get("executions", []))
        ],
        "open_trades": [],
    }

    flex_result = asyncio.run(run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120))
    if not flex_result.ok:
        print(json.dumps({"skipped": True, "reason": "flex_unavailable"}, indent=2))
        return 0

    summary = compute_divergence(pg_filtered, flex_result.data or {})
    if args.apply:
        run_id = record_run(scope=scope, summary=summary)
        print(json.dumps({"applied": True, "run_id": run_id, **summary}, indent=2))
    else:
        print(json.dumps({"dry_run": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

Run the tests, expect all 4 pass.

- [ ] **Step 5.5: Add `/health.flex_divergence` field**

In `src/xenon/api/server.py`, find `_snapshotter_health()` (around line 1129). Below it, add:

```python
def _flex_divergence_health() -> dict:
    """Latest nightly PG↔Flex divergence run, if any. Resolves scope via app.state."""
    try:
        from xenon.jobs.flex_divergence_check import latest_run
        from xenon.execution.account_scope import AccountScope
        kwargs = _resolve_scope_kwargs()
        scope = AccountScope(broker=kwargs["broker"], account_env=kwargs["account_env"], broker_account=kwargs["broker_account"])
        row = latest_run(scope=scope)
    except Exception:  # noqa: BLE001
        return {"configured": False}
    if row is None:
        return {"configured": True, "ran_at": None, "divergence_count": None}
    return {
        "configured": True,
        "ran_at": row["ran_at"].isoformat() if row.get("ran_at") else None,
        "total_compared": row["total_compared"],
        "divergence_count": row["divergence_count"],
    }
```

Note: `_resolve_scope_kwargs()` already exists in `server.py` (around line 2064) and resolves scope from `app.state` with safe fallback — exactly what is needed in a request handler.

In `health()` (around line 1167), add the field to the returned dict:

```python
        "flex_divergence": _flex_divergence_health(),
```

- [ ] **Step 5.6: Test the new health field**

Add to `src/xenon/api/tests/test_health_observability.py` (or create a focused new file):

```python
def test_health_includes_flex_divergence_field(client):
    response = client.get("/health")
    body = response.json()
    assert "flex_divergence" in body
    assert "configured" in body["flex_divergence"]
```

Run: `uv run pytest src/xenon/api/tests/test_health_observability.py -v`
Expected: all green.

- [ ] **Step 5.7: Document the cron entry**

Append to `docs/runbooks/ops.md`:

```markdown
### Nightly Flex divergence

Cron entry — runs once a day after 18:00 ET on weekdays:

    0 23 * * 1-5  cd /opt/xenon && XENON_TRADING_MODE=live XENON_BROKER_ACCOUNT=$LIVE_ACCT \
                  uv run python -m xenon.jobs.flex_divergence_check --apply

The job filters PG trades by yesterday's NYSE session window (`yesterday 00:00 ET`
to `today 00:00 ET`), compares against IB Flex same-day output, writes one row
to `xenon.flex_divergence_runs`, and is surfaced on `GET /health.flex_divergence`.

Skipped silently when `IB_FLEX_TOKEN` / `IB_FLEX_QUERY_ID` are unset.
```

- [ ] **Step 5.8: Commit**

```bash
git add src/xenon/db/schema.py \
        src/xenon/db/migrations/versions/*flex_divergence_runs*.py \
        src/xenon/jobs/__init__.py \
        src/xenon/jobs/flex_divergence_check.py \
        src/xenon/api/server.py \
        src/xenon/api/tests/test_health_observability.py \
        scripts/tests/test_flex_divergence_check.py \
        docs/runbooks/ops.md
git commit -m "feat(observability): nightly PG-Flex divergence on /health (V.4)"
```

---

## Final verification (after all 6 tasks)

- [ ] **Python tests:**

```bash
uv run pytest src/xenon/db/tests src/xenon/api/tests \
              scripts/tests/test_blotter_merge.py \
              scripts/tests/test_blotter_pg_perm_id.py \
              scripts/tests/test_flex_query_perm_id.py \
              scripts/tests/test_replay_unknown_orders.py \
              scripts/tests/test_flex_divergence_check.py -q
```

- [ ] **Web tests:**

```bash
cd web && npx vitest run web/tests/blotter-route-pg.test.ts \
                          web/tests/historical-trades-source-pill.test.tsx
```

- [ ] **Order-path CI guards still pass:**

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
uv run python scripts/checks/order_path_caller_allowlist.py
```

- [ ] **Alembic state:**

```bash
uv run alembic upgrade head
uv run alembic check
```

- [ ] **Push and update PR description:**

```bash
git push origin feat/postgres-migration-completion
```

PR description addendum:

> Loose ends closed by this branch (per `docs/superpowers/plans/2026-04-29-postgres-migration-loose-ends.md`):
>
> - W4.7 — IB_AUTO_IMPORT journal entries via PG `trade.closed` listener (NOTIFY-id fetch + boot replay + consumed_by ack + concurrent-safe upsert)
> - W3.4-prereq — `perm_id` plumbed through PG and Flex blotter payloads
> - W1.3 — Replay script for pre-existing UNKNOWN order_submissions (state-parameterized rehydrate; surfaces errors)
> - W3.4 — PG+Flex blotter overlay merge with per-row divergence and recomputed summary
> - W3.6 — Source pill (PG / FLEX / PG+FLEX) in HistoricalTradesSection
> - V.4 — Nightly PG↔Flex divergence on `/health.flex_divergence`, NYSE-session calendar bound

---

## Notes for the executing engineer

- One commit per task. Do not batch.
- TDD: red, green, refactor. Tests first.
- All Python via `uv` per `CLAUDE.md`.
- `run_module` is at `xenon.api.subprocess` (verified).
- The plan's `replay_states` import in `single_leg_rehydrate.py` may need small wiring tweaks (the file's existing IB-client factory variable name varies by convention) — read the file's `rehydrate_on_boot` once to see how it gets the IB client, and reuse that exact path in `replay_states`.
- Visual verification (Task 4 Step 4.4) is mandatory per `web/CLAUDE.md`. Don't skip even if unit tests are green.
- This plan was reviewed by a Codex/Gemini/Claude tribunal on 2026-04-29; v1 had 15 findings, all addressed in this v2. The full triage is in the conversation that produced this file.
