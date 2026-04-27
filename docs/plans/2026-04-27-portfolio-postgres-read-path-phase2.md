# Portfolio Postgres Read Path — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the remaining 12 Python readers off `data/portfolio.json` so the file is never read at runtime, then delete it from the repo and the runtime hot path.

**Architecture:** Phase 1 (PR #56) already populates `xenon.account_snapshots.payload` (jsonb) with the full UI-shaped portfolio dict at sync time, and exposes `get_latest_portfolio_payload(scope)` for async callers. This phase adds a sync sibling for subprocess/CLI callers, then migrates each reader site to use one of the two loaders. Safety-critical readers (preflight, naked-short audit, ib_reconcile, incremental_sync, ib_sync's own self-read) MUST be scope-aware to preserve broker-account isolation; discovery readers (scanners, analyst ratings, UW candidates, reports) read the latest payload regardless of scope.

**Tech Stack:** Python 3.13, SQLAlchemy 2 (sync engine via `get_sync_engine()`), Postgres jsonb, pytest with the existing alembic-managed test schema.

**Branch:** `fix/portfolio-postgres-read-path-phase2`

**Predecessor plan:** `docs/plans/2026-04-27-portfolio-postgres-read-path.md` (Phase 1 — IB writer + FastAPI `/portfolio` route + UI). This file extends it.

**Out of scope:** Futu writes (`data/futu_portfolio.json`) — tracked in `docs/plans/2026-04-27-futu-postgres-migration-followup.md`. `portfolio_adapter._normalize_futu` keeps reading the Futu JSON until that follow-up lands.

---

## File Structure

**Created:**

- `src/xenon/utils/portfolio_loader.py` — single source of truth for "load the latest IB portfolio payload from PG". Two entry points: `load_portfolio_payload_sync(scope=None)` and `get_portfolio_tickers_sync(scope=None)`. Async siblings already exist in `src/xenon/db/queries/portfolio.py`.
- `scripts/tests/test_portfolio_loader.py` — direct unit tests for the new module (fixtures + scope filtering).
- `scripts/tests/helpers/portfolio_seed.py` — test helper that inserts an `account_snapshots` row with a given payload and scope. Used across all migration tasks so we don't repeat insert-statement boilerplate.

**Modified (one reader site per task):**

- `src/xenon/api/server.py` — `_load_portfolio_view()` (line 1465-1483) reads PG, uses `app.state` scope.
- `src/xenon/execution/naked_short_audit.py` — `main()` (line 295-330) drops `--portfolio` file load.
- `src/xenon/execution/ib_sync.py` — own self-read for entry_date carry-forward (line 990-1003).
- `src/xenon/execution/ib_reconcile.py` — `main()` (line 273-301).
- `src/xenon/utils/incremental_sync.py` — `incremental_sync()` (line 92-126) takes a payload, not a path.
- `src/xenon/utils/portfolio_adapter.py` — `load_normalized_positions("ib", ...)` reads PG; Futu branch unchanged.
- `src/xenon/fetchers/fetch_analyst_ratings.py` — `get_portfolio_tickers()` (line ~56-65).
- `src/xenon/scanners/scanner.py` — `get_open_positions()` (line ~28-35).
- `src/xenon/scanners/discover.py` — wherever `PORTFOLIO` is read.
- `src/xenon/scanners/leap_iv.py` — `load_portfolio_tickers()` (line ~982-995).
- `src/xenon/api/services/uw_analyze_candidates.py` — `PORTFOLIO_PATH` reader (line ~25).
- `src/xenon/reports/free_trade_analyzer.py`, `portfolio_performance.py:1344`, `scenario_analysis.py:732`.

**Deleted (final task):**

- `data/portfolio.json` — removed from working tree.

---

## Task 1: Shared sync loader module

**Files:**

- Create: `src/xenon/utils/portfolio_loader.py`
- Test: `scripts/tests/test_portfolio_loader.py`
- Create: `scripts/tests/helpers/portfolio_seed.py`

- [ ] **Step 1.1: Write the test helper**

```python
# scripts/tests/helpers/portfolio_seed.py
"""Test helper: seed xenon.account_snapshots.payload for portfolio_loader tests.

Used by every reader-migration task in
docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots


def seed_portfolio_snapshot(
    payload: dict[str, Any],
    *,
    broker: str = "IB",
    account_env: str = "live",
    broker_account: str = "U1234567",
    bankroll: Decimal | float = 100_000,
    peak_value: Decimal | float = 100_000,
    net_liquidation: Decimal | float = 100_000,
) -> None:
    """Insert one account_snapshots row. Caller controls scope and payload."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=broker_account,
                bankroll=Decimal(str(bankroll)),
                peak_value=Decimal(str(peak_value)),
                net_liquidation=Decimal(str(net_liquidation)),
                payload=payload,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )
        )
```

- [ ] **Step 1.2: Write the failing tests**

```python
# scripts/tests/test_portfolio_loader.py
"""Tests for portfolio_loader.load_portfolio_payload_sync."""
from __future__ import annotations

import pytest

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import (
    get_portfolio_tickers_sync,
    load_portfolio_payload_sync,
)


PAYLOAD_LIVE = {
    "positions": [{"ticker": "AAPL", "structure": "Stock", "legs": []}],
    "bankroll": 100_000,
}
PAYLOAD_PAPER = {
    "positions": [{"ticker": "MSFT", "structure": "Stock", "legs": []}],
    "bankroll": 50_000,
}


def test_returns_none_when_no_snapshots(pg_clean):
    assert load_portfolio_payload_sync() is None


def test_returns_latest_when_scope_none(pg_clean):
    seed_portfolio_snapshot(PAYLOAD_PAPER, account_env="paper", broker_account="DU111")
    seed_portfolio_snapshot(PAYLOAD_LIVE, account_env="live", broker_account="U222")
    payload = load_portfolio_payload_sync()
    assert payload == PAYLOAD_LIVE  # most recent insert wins


def test_filters_by_scope(pg_clean):
    seed_portfolio_snapshot(PAYLOAD_PAPER, account_env="paper", broker_account="DU111")
    seed_portfolio_snapshot(PAYLOAD_LIVE, account_env="live", broker_account="U222")
    paper_scope = AccountScope(broker="IB", account_env="paper", broker_account="DU111")
    live_scope = AccountScope(broker="IB", account_env="live", broker_account="U222")
    assert load_portfolio_payload_sync(scope=paper_scope) == PAYLOAD_PAPER
    assert load_portfolio_payload_sync(scope=live_scope) == PAYLOAD_LIVE


def test_get_portfolio_tickers_sync_extracts_unique_tickers(pg_clean):
    seed_portfolio_snapshot({
        "positions": [
            {"ticker": "AAPL", "legs": []},
            {"ticker": "aapl", "legs": []},  # case fold
            {"ticker": "MSFT", "legs": []},
        ],
    })
    assert get_portfolio_tickers_sync() == ["AAPL", "MSFT"]


def test_get_portfolio_tickers_sync_returns_empty_when_no_snapshot(pg_clean):
    assert get_portfolio_tickers_sync() == []
```

The `pg_clean` fixture is defined in `scripts/tests/conftest.py`; it truncates `xenon.account_snapshots` before each test. If it doesn't exist yet, add it in this task:

```python
# scripts/tests/conftest.py — append
import pytest
from sqlalchemy import text
from xenon.db.engine import get_sync_engine

@pytest.fixture
def pg_clean():
    """Truncate portfolio-related tables before/after a test."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.account_snapshots, xenon.positions RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.account_snapshots, xenon.positions RESTART IDENTITY CASCADE"))
```

- [ ] **Step 1.3: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_portfolio_loader.py -v`
Expected: 5 failures with `ImportError: cannot import name 'load_portfolio_payload_sync' from 'xenon.utils.portfolio_loader'`.

- [ ] **Step 1.4: Write the implementation**

```python
# src/xenon/utils/portfolio_loader.py
"""Single read seam for the IB portfolio payload from Postgres.

Phase 2 of the postgres-read-path migration — see
docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md.

Sync-only on purpose: the FastAPI hot path already has
`xenon.db.queries.portfolio.get_latest_portfolio_payload` (async). This
module exists for sync subprocesses (ib_sync, ib_reconcile,
naked_short_audit), CLI scripts (scanners, ratings, reports), and
non-async services (uw_analyze_candidates).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots
from xenon.execution.account_scope import AccountScope


def load_portfolio_payload_sync(*, scope: AccountScope | None = None) -> dict[str, Any] | None:
    """Return the latest IB portfolio payload from Postgres, or None.

    When `scope` is provided, only rows matching that broker/env/account
    are considered. This is mandatory for safety-critical readers
    (preflight, naked-short audit, reconcile) that must not blend
    paper/live data.

    When `scope` is None, returns the absolute latest snapshot regardless
    of scope. Use only for scope-naive discovery (scanner ticker sets,
    analyst-ratings target list, UW candidate underlyings) where mixing
    accounts is acceptable.
    """
    stmt = select(account_snapshots.c.payload).order_by(
        account_snapshots.c.snapshot_at.desc()
    ).limit(1)
    if scope is not None:
        stmt = (
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == scope.broker)
            .where(account_snapshots.c.account_env == scope.account_env)
            .where(account_snapshots.c.broker_account == scope.broker_account)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None
    payload = row.payload or {}
    return dict(payload) if payload else None


def get_portfolio_tickers_sync(*, scope: AccountScope | None = None) -> list[str]:
    """Convenience wrapper: extract unique uppercase tickers from the latest snapshot."""
    payload = load_portfolio_payload_sync(scope=scope)
    if not payload:
        return []
    seen: set[str] = set()
    for pos in payload.get("positions", []):
        ticker = str(pos.get("ticker", "")).upper().strip()
        if ticker:
            seen.add(ticker)
    return sorted(seen)
```

- [ ] **Step 1.5: Run tests to verify they pass**

Run: `uv run pytest scripts/tests/test_portfolio_loader.py -v`
Expected: 5 PASS.

- [ ] **Step 1.6: Commit**

```bash
git add src/xenon/utils/portfolio_loader.py \
        scripts/tests/test_portfolio_loader.py \
        scripts/tests/helpers/portfolio_seed.py \
        scripts/tests/conftest.py
git commit -m "feat(portfolio): sync postgres loader for non-async readers"
```

---

## Task 2: Migrate preflight (`api/server.py:_load_portfolio_view`)

**Why first:** preflight gates `/orders/place`. Reading a frozen JSON snapshot here means Gate 4 evaluates against potentially-2-day-old positions during a live trading session — the exact bug PR #56 was scoped to address but only fixed UI-side.

**Files:**

- Modify: `src/xenon/api/server.py:1465-1483` (`_load_portfolio_view`)
- Test: `src/xenon/api/tests/test_preflight_route.py` (existing — extend)

- [ ] **Step 2.1: Add a failing test for the PG read path**

Append to `src/xenon/api/tests/test_preflight_route.py`:

```python
def test_load_portfolio_view_reads_from_postgres(client_with_scope, pg_clean):
    """Regression: _load_portfolio_view must read PG, not data/portfolio.json."""
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot

    seed_portfolio_snapshot({
        "positions": [{
            "ticker": "AAPL",
            "structure_type": "LONG_STOCK",
            "contracts": 100,
            "expiry": None,
            "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0}],
        }],
        "available_funds": "5000",
    }, broker="IB", account_env="paper", broker_account="DU111")

    # SELL 50 AAPL shares — should pass (100 held).
    resp = client_with_scope.post("/orders/place", json={
        "symbol": "AAPL", "type": "stock", "action": "SELL", "quantity": 50,
        "limitPrice": "190", "clientAttemptId": "test-1",
    })
    # The point is preflight didn't reject on UNIVERSE_UNKNOWN/INSUFFICIENT_SHARES
    # because it found the 100 long shares from PG.
    assert resp.status_code != 403 or "INSUFFICIENT_SHARES" not in resp.text
```

If `client_with_scope` doesn't yet exist as a fixture, add it (mirror existing TestClient lifespan-aware helper that pre-seeds `app.state.trading_mode='paper'` and `app.state.account='DU111'`).

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_preflight_route.py::test_load_portfolio_view_reads_from_postgres -xvs`
Expected: fails because `_load_portfolio_view` still hits the missing `data/portfolio.json`.

- [ ] **Step 2.3: Migrate `_load_portfolio_view`**

Replace `src/xenon/api/server.py:1465-1483`:

```python
def _load_portfolio_view(app_state=None) -> PortfolioView | None:
    """Load portfolio snapshot from Postgres for preflight Gate 4.

    Reads the scope-specific payload via portfolio_loader so paper/live
    data never blend. Returns None when no snapshot exists; the caller
    fails OPEN to mirror web/app/api/orders/place/route.ts behavior on
    fresh environments.
    """
    from xenon.execution.account_scope import resolve_from_app_state
    from xenon.utils.portfolio_loader import load_portfolio_payload_sync

    if app_state is None:
        return None
    try:
        scope = resolve_from_app_state(app_state)
    except ValueError as exc:
        logger.warning("[preflight] could not resolve scope: %s", exc)
        return None
    try:
        payload = load_portfolio_payload_sync(scope=scope)
    except Exception as exc:  # noqa: BLE001 — fail open on DB error
        logger.warning("[preflight] could not load portfolio from PG: %s", exc)
        return None
    if not payload:
        return None
    try:
        return PortfolioView.model_validate(payload)
    except ValidationError as exc:
        logger.warning("[preflight] payload failed PortfolioView validation: %s", exc)
        return None
```

There is exactly one callsite, at `src/xenon/api/server.py:1519`. Change:

```python
portfolio = _load_portfolio_view()
```

to:

```python
portfolio = _load_portfolio_view(request.app.state)
```

(`request` is already in scope at that callsite — it's the FastAPI handler parameter.)

- [ ] **Step 2.4: Run preflight + route tests**

Run: `uv run pytest src/xenon/api/tests/test_preflight_route.py scripts/tests/test_preflight.py -v`
Expected: all PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_preflight_route.py
git commit -m "fix(preflight): read portfolio from postgres scoped by app.state"
```

---

## Task 3: Migrate naked_short_audit

**Files:**

- Modify: `src/xenon/execution/naked_short_audit.py:295-330` (`main`)
- Test: `scripts/tests/test_naked_short_audit.py` (existing — extend)

- [ ] **Step 3.1: Failing test**

Append to `scripts/tests/test_naked_short_audit.py`:

```python
def test_main_reads_positions_from_postgres(pg_clean, monkeypatch, tmp_path):
    """naked_short_audit.main() must source positions from PG, not portfolio.json."""
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.execution.naked_short_audit import main

    seed_portfolio_snapshot({
        "positions": [{
            "ticker": "AAPL",
            "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0}],
        }],
    }, broker="IB", account_env="paper", broker_account="DU111")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU111")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")

    orders_path = tmp_path / "orders.json"
    orders_path.write_text('{"open_orders": []}')

    summary = main(["--dry-run", "--orders", str(orders_path)])
    assert summary["positions_loaded"] == 1
    assert summary["violations"] == 0
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_naked_short_audit.py::test_main_reads_positions_from_postgres -xvs`
Expected: FAIL — main still requires `--portfolio` path.

- [ ] **Step 3.3: Migrate `main()`**

In `src/xenon/execution/naked_short_audit.py`, replace the argparse + load block (lines 297-315 in current file) with:

```python
def main(argv=None):
    """CLI entry point. Returns summary dict (for testing)."""
    parser = argparse.ArgumentParser(description="Naked short audit — detect and cancel violations")
    parser.add_argument("--dry-run", action="store_true", help="Print violations without cancelling")
    parser.add_argument("--orders", type=str, default=str(DATA_DIR / "orders.json"), help="Path to orders.json")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT)

    args = parser.parse_args(argv)

    from xenon.execution.account_scope import resolve_from_env
    from xenon.utils.portfolio_loader import load_portfolio_payload_sync

    scope = resolve_from_env()
    payload = load_portfolio_payload_sync(scope=scope)
    if payload is None:
        log("No portfolio snapshot in Postgres for this scope — skipping audit", "warn")
        return {"positions_loaded": 0, "violations": 0}
    positions = payload.get("positions", [])

    with open(args.orders) as f:
        orders_data = json.load(f)
    orders = orders_data.get("open_orders", [])

    violations = find_naked_short_violations(orders, positions)
```

(Keep the rest of `main()` from `# Detect violations` onward unchanged. The orders.json read stays — orders migration is a separate follow-up. The `--portfolio` arg is gone.)

Also update the `from naked_short_audit import find_naked_short_violations` call inside `ib_sync.py` (post-sync audit, line ~1383): pass `portfolio["positions"]` directly — it already does this, so only the CLI surface changes.

- [ ] **Step 3.4: Run audit tests**

Run: `uv run pytest scripts/tests/test_naked_short_audit.py -v`
Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/xenon/execution/naked_short_audit.py scripts/tests/test_naked_short_audit.py
git commit -m "fix(naked-short-audit): read positions from postgres, drop --portfolio arg"
```

---

## Task 4: Migrate ib_sync's own previous-snapshot read

**Context:** `ib_sync.py:990-1003` reads `portfolio.json` to recover `entry_date` carry-forward (so we don't reset `entry_date = today` on every sync). After the JSON write was removed in PR #56, this read silently sees a frozen file → eventually nothing → entry_date defaults churn back. Migrate to PG.

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:988-1004`
- Test: `scripts/tests/test_ib_sync_entry_date.py` (existing — extend)

- [ ] **Step 4.1: Failing test**

Append a test that:

1. Seeds a PG snapshot with a prior position carrying `entry_date='2026-04-20'`.
2. Calls `convert_to_portfolio_format(...)` for that position with today's date.
3. Asserts the resulting position keeps `entry_date='2026-04-20'`, not today.

```python
def test_entry_date_carries_forward_from_postgres(pg_clean, monkeypatch):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.execution.ib_sync import convert_to_portfolio_format

    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU111")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")

    seed_portfolio_snapshot({
        "positions": [{
            "ticker": "AAPL",
            "structure": "Stock",
            "expiry": "N/A",
            "entry_date": "2026-04-20",
            "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0}],
        }],
    }, broker="IB", account_env="paper", broker_account="DU111")

    # Build the same collapsed-position shape today
    today_collapsed = [{
        "ticker": "AAPL", "structure": "Stock", "expiry": "N/A",
        "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0}],
    }]
    result = convert_to_portfolio_format({}, today_collapsed, {}, fill_dates={})
    pos = result["positions"][0]
    assert pos["entry_date"] == "2026-04-20"
```

- [ ] **Step 4.2: Run test, expect FAIL**

Run: `uv run pytest scripts/tests/test_ib_sync_entry_date.py::test_entry_date_carries_forward_from_postgres -xvs`

- [ ] **Step 4.3: Migrate the read**

Replace `src/xenon/execution/ib_sync.py:990-1003`:

```python
# Previous portfolio dates (fallback) — read from PG, not stale JSON.
prev_dates: dict[str, str] = {}
try:
    from xenon.execution.account_scope import resolve_from_env
    from xenon.utils.portfolio_loader import load_portfolio_payload_sync

    prev_payload = load_portfolio_payload_sync(scope=resolve_from_env())
    for p in (prev_payload or {}).get("positions", []):
        key = f"{p.get('ticker')}|{p.get('structure')}|{p.get('expiry')}"
        ed = p.get("entry_date", "")
        if ed and ed != today:
            prev_dates[key] = ed
except Exception:
    pass  # treat missing snapshot the same as missing JSON file before
```

Drop the unused `_json` import if it becomes orphaned, and remove the `PORTFOLIO_PATH.exists() / .read_text()` block.

- [ ] **Step 4.4: Run tests**

Run: `uv run pytest scripts/tests/test_ib_sync_entry_date.py -v`
Expected: PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_entry_date.py
git commit -m "fix(ib-sync): read previous-snapshot entry dates from postgres"
```

---

## Task 5: Migrate ib_reconcile

**Files:**

- Modify: `src/xenon/execution/ib_reconcile.py:273-301`
- Test: `scripts/tests/test_ib_reconcile.py` (existing — extend)

- [ ] **Step 5.1: Failing test**

```python
def test_main_reads_positions_from_postgres(pg_clean, monkeypatch, mock_ib_client):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.execution.ib_reconcile import main

    seed_portfolio_snapshot({
        "positions": [{"ticker": "AAPL", "legs": [
            {"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0}
        ]}],
    }, broker="IB", account_env="paper", broker_account="DU111")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU111")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")

    # mock_ib_client returns matching positions → no discrepancies
    main()  # should NOT crash on missing portfolio.json
```

- [ ] **Step 5.2: Run, expect FAIL**

- [ ] **Step 5.3: Migrate**

Replace `src/xenon/execution/ib_reconcile.py:289-301` (the `verified_load(portfolio_path)` block):

```python
from xenon.execution.account_scope import resolve_from_env
from xenon.utils.portfolio_loader import load_portfolio_payload_sync

try:
    portfolio = load_portfolio_payload_sync(scope=resolve_from_env()) or {}
except ValueError as exc:
    log(f"Could not resolve account scope for reconcile: {exc}", "warn")
    portfolio = {}
```

Remove the `portfolio_path = project_root / "data" / "portfolio.json"` line and the `verified_load` import if unused elsewhere in the file.

- [ ] **Step 5.4: Run tests**

Run: `uv run pytest scripts/tests/test_ib_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/xenon/execution/ib_reconcile.py scripts/tests/test_ib_reconcile.py
git commit -m "fix(ib-reconcile): source local portfolio from postgres"
```

---

## Task 6: Migrate `incremental_sync`

**Context:** `incremental_sync.py` is called from `ib_sync.py` to decide whether a full sync is needed. Currently takes `portfolio_path: Path`. Refactor to take a payload dict.

**Files:**

- Modify: `src/xenon/utils/incremental_sync.py` (whole module)
- Modify: callsite in `src/xenon/execution/ib_sync.py` (search `incremental_sync(`)
- Test: `scripts/tests/test_incremental_sync.py`

- [ ] **Step 6.1: Failing test**

```python
def test_incremental_sync_takes_payload_dict():
    from xenon.utils.incremental_sync import incremental_sync

    fake_client = FakeClient(positions=[
        FakePosition("AAPL", "STK", "", 100),
    ])
    payload = {"positions": [{"ticker": "AAPL", "expiry": "N/A", "contracts": 100}]}
    result = incremental_sync(fake_client, payload=payload)
    assert result["changed"] is False
    assert result["portfolio"] == payload
```

- [ ] **Step 6.2: Run, expect FAIL** (signature mismatch).

- [ ] **Step 6.3: Update signature**

In `src/xenon/utils/incremental_sync.py`, replace `incremental_sync(client, portfolio_path)` with:

```python
def incremental_sync(client: Any, *, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Compare current portfolio against IB and decide if full sync is needed.

    Args:
        client: IBClient instance (must be connected).
        payload: latest portfolio payload from PG (None when no snapshot exists).

    Returns:
        dict with keys:
            - changed (bool): True if positions differ.
            - portfolio (dict or None): existing payload when unchanged.
    """
    ib_positions = client.get_positions()
    if payload is None:
        logger.info("No portfolio snapshot — full sync needed")
        return {"changed": True, "portfolio": None}

    existing_positions = payload.get("positions", [])
    if positions_changed(existing_positions, ib_positions):
        logger.info("Position changes detected — full sync needed")
        return {"changed": True, "portfolio": payload}

    logger.info("No position changes detected — skipping full sync")
    return {"changed": False, "portfolio": payload}
```

In `src/xenon/execution/ib_sync.py`, update the callsite:

```python
from xenon.execution.account_scope import resolve_from_env
from xenon.utils.portfolio_loader import load_portfolio_payload_sync

prev_payload = load_portfolio_payload_sync(scope=resolve_from_env())
result = incremental_sync(client, payload=prev_payload)
```

- [ ] **Step 6.4: Run tests**

Run: `uv run pytest scripts/tests/test_incremental_sync.py -v`
Expected: PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/xenon/utils/incremental_sync.py src/xenon/execution/ib_sync.py scripts/tests/test_incremental_sync.py
git commit -m "refactor(incremental-sync): accept payload dict instead of file path"
```

---

## Task 7: Migrate `portfolio_adapter` (IB branch only)

**Files:**

- Modify: `src/xenon/utils/portfolio_adapter.py:126-140`
- Test: `scripts/tests/test_portfolio_adapter.py` (existing — extend)

- [ ] **Step 7.1: Failing test**

```python
def test_load_normalized_positions_ib_reads_postgres(pg_clean):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.utils.portfolio_adapter import load_normalized_positions

    seed_portfolio_snapshot({
        "positions": [{
            "ticker": "AAPL", "direction": "LONG", "structure": "Stock", "contracts": 100,
            "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100}],
        }],
    })
    result = load_normalized_positions("ib")
    assert len(result.positions) == 1
    assert result.positions[0].ticker == "AAPL"
```

- [ ] **Step 7.2: Run, expect FAIL** (currently reads JSON file that's missing).

- [ ] **Step 7.3: Migrate the IB branch**

In `src/xenon/utils/portfolio_adapter.py:126-140`:

```python
def load_normalized_positions(account: Account) -> LoadResult:
    """Load and normalize positions for the given account.

    IB reads from Postgres (latest snapshot, scope-naive). Futu still
    reads `data/futu_portfolio.json` until the Futu→PG migration ships
    (docs/plans/2026-04-27-futu-postgres-migration-followup.md).
    """
    if account == "ib":
        from xenon.utils.portfolio_loader import load_portfolio_payload_sync

        payload = load_portfolio_payload_sync() or {}
        rows = payload.get("positions", []) if isinstance(payload, dict) else []
        return _normalize_ib(rows)
    if account == "futu":
        data = _read_json(FUTU_PORTFOLIO)
        rows = data.get("positions", []) if isinstance(data, dict) else []
        return _normalize_futu(rows)
    raise ValueError(f"Unknown account: {account!r} (expected 'ib' or 'futu')")
```

Drop the `IB_PORTFOLIO` constant + the now-unused `_read_json` for IB. Keep `_read_json` for the Futu branch.

- [ ] **Step 7.4: Run tests**

Run: `uv run pytest scripts/tests/test_portfolio_adapter.py -v`
Expected: PASS (Futu tests untouched).

- [ ] **Step 7.5: Commit**

```bash
git add src/xenon/utils/portfolio_adapter.py scripts/tests/test_portfolio_adapter.py
git commit -m "fix(portfolio-adapter): IB branch reads postgres; futu unchanged"
```

---

## Task 8: Migrate `fetch_analyst_ratings.get_portfolio_tickers`

**Files:**

- Modify: `src/xenon/fetchers/fetch_analyst_ratings.py` (top constants + `get_portfolio_tickers`)
- Test: `scripts/tests/test_fetch_analyst_ratings.py` (existing — extend)

- [ ] **Step 8.1: Failing test**

```python
def test_get_portfolio_tickers_reads_postgres(pg_clean):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.fetchers.fetch_analyst_ratings import get_portfolio_tickers

    seed_portfolio_snapshot({"positions": [
        {"ticker": "AAPL", "legs": []},
        {"ticker": "msft", "legs": []},
    ]})
    assert get_portfolio_tickers() == ["AAPL", "MSFT"]
```

- [ ] **Step 8.2: Run, expect FAIL**.

- [ ] **Step 8.3: Migrate**

Replace the `get_portfolio_tickers()` body in `src/xenon/fetchers/fetch_analyst_ratings.py`:

```python
def get_portfolio_tickers() -> list:
    """Extract all tickers from portfolio (Postgres latest snapshot, any scope)."""
    from xenon.utils.portfolio_loader import get_portfolio_tickers_sync

    return get_portfolio_tickers_sync()
```

Delete the `PORTFOLIO_FILE = DATA_DIR / "portfolio.json"` constant if no other reference remains.

- [ ] **Step 8.4: Run tests**

Run: `uv run pytest scripts/tests/test_fetch_analyst_ratings.py -v`
Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/xenon/fetchers/fetch_analyst_ratings.py scripts/tests/test_fetch_analyst_ratings.py
git commit -m "fix(analyst-ratings): pull portfolio tickers from postgres"
```

---

## Task 9: Migrate scanners (`scanner.py`, `discover.py`, `leap_iv.py`)

**Files:**

- Modify: `src/xenon/scanners/scanner.py:28, get_open_positions`
- Modify: `src/xenon/scanners/discover.py` (search `PORTFOLIO`)
- Modify: `src/xenon/scanners/leap_iv.py:982-995` (`load_portfolio_tickers`)
- Tests: `scripts/tests/test_scanner_refactor.py`, `scripts/tests/test_discover.py` (or equivalent), `scripts/tests/test_leap_iv.py`

- [ ] **Step 9.1: Failing test in `scripts/tests/test_scanner_refactor.py`**

```python
def test_get_open_positions_reads_postgres(pg_clean):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.scanners.scanner import get_open_positions

    seed_portfolio_snapshot({"positions": [
        {"ticker": "AAPL", "legs": []},
        {"ticker": "MSFT", "legs": []},
    ]})
    assert get_open_positions() == {"AAPL", "MSFT"}
```

- [ ] **Step 9.2: Migrate `scanner.get_open_positions`**

Replace lines 28-35 in `src/xenon/scanners/scanner.py`:

```python
def get_open_positions():
    """Set of tickers with open positions (latest PG snapshot, any scope)."""
    from xenon.utils.portfolio_loader import get_portfolio_tickers_sync

    return set(get_portfolio_tickers_sync())
```

Drop `PORTFOLIO = PROJECT_DIR / "data" / "portfolio.json"`.

- [ ] **Step 9.3: Clean up `discover.py`**

`src/xenon/scanners/discover.py:41` declares:

```python
PORTFOLIO = Path(__file__).resolve().parent.parent.parent.parent / "data" / "portfolio.json"
```

The constant is **never read** elsewhere in the file — it's dead code. Just delete the line. No other migration needed for this module.

Verify: `grep -n "PORTFOLIO" src/xenon/scanners/discover.py` should return zero matches after the deletion.

- [ ] **Step 9.4: Migrate `leap_iv.py:982-995`**

```python
def load_portfolio_tickers() -> list[str]:
    from xenon.utils.portfolio_loader import get_portfolio_tickers_sync

    tickers = get_portfolio_tickers_sync()
    if not tickers:
        print("⚠ No portfolio snapshot in Postgres")
    return tickers
```

- [ ] **Step 9.5: Run scanner tests**

Run: `uv run pytest scripts/tests/test_scanner_refactor.py scripts/tests/test_leap_iv.py -v` (and any discover test file).
Expected: PASS.

- [ ] **Step 9.6: Commit**

```bash
git add src/xenon/scanners/ scripts/tests/
git commit -m "fix(scanners): pull portfolio underlyings from postgres"
```

---

## Task 10: Migrate `uw_analyze_candidates`

**Files:**

- Modify: `src/xenon/api/services/uw_analyze_candidates.py:25` + reader function
- Test: `scripts/tests/test_uw_analyze_candidates.py`

- [ ] **Step 10.1: Failing test**

```python
def test_portfolio_tickers_reads_postgres(pg_clean):
    from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
    from xenon.api.services.uw_analyze_candidates import portfolio_tickers, seed_candidates

    seed_portfolio_snapshot({"positions": [
        {"ticker": "AAPL", "legs": []},
        {"ticker": "msft", "legs": []},
    ]})
    assert portfolio_tickers() == {"AAPL", "MSFT"}
    candidates = seed_candidates()
    assert "portfolio" in candidates["AAPL"]
```

- [ ] **Step 10.2: Run, expect FAIL**

- [ ] **Step 10.3: Migrate**

In `src/xenon/api/services/uw_analyze_candidates.py`:

1. Replace the `portfolio_tickers` function (line 88-99) with:

```python
def portfolio_tickers() -> set[str]:
    """Return uppercase tickers from the latest IB portfolio snapshot in Postgres."""
    from xenon.utils.portfolio_loader import get_portfolio_tickers_sync

    return set(get_portfolio_tickers_sync())
```

Note: drop the `path` parameter — there's no file path anymore.

2. Update `seed_candidates` (line 116-130). Drop `portfolio_path` from the signature and the `portfolio_path or PORTFOLIO_PATH` callsite at line 128:

```python
def seed_candidates(
    *,
    watchlist_path: Path | None = None,
    extra_adhoc: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Build the ticker → sources[] map.

    Watchlist path defaults to the module-level `WATCHLIST_PATH` resolved AT
    CALL TIME — tests rebinding this attribute will be picked up correctly.
    Portfolio tickers come from Postgres (latest snapshot, scope-naive).
    """
    port = portfolio_tickers()
    watch = watchlist_tickers(watchlist_path or WATCHLIST_PATH)
    # ...rest unchanged
```

3. Delete the `PORTFOLIO_PATH = _DATA / "portfolio.json"` line at the top of the module.

4. Audit callers of `seed_candidates(portfolio_path=...)` — `grep -rn "seed_candidates(" src/ scripts/` — and remove the kwarg.

- [ ] **Step 10.4: Run tests**

Run: `uv run pytest scripts/tests/test_uw_analyze_candidates.py -v`

- [ ] **Step 10.5: Commit**

```bash
git add src/xenon/api/services/uw_analyze_candidates.py scripts/tests/test_uw_analyze_candidates.py
git commit -m "fix(uw-analyze): read portfolio underlyings from postgres"
```

---

## Task 11: Migrate reports (`free_trade_analyzer`, `portfolio_performance`, `scenario_analysis`)

**Files:**

- Modify: `src/xenon/reports/free_trade_analyzer.py` — drop `PORTFOLIO_FILE` (line 34); replace the load block at lines 324-331 (`if not PORTFOLIO_FILE.exists(): ... verified_load(...) ... with open(PORTFOLIO_FILE) as f: ...`).
- Modify: `src/xenon/reports/portfolio_performance.py` — drop `PORTFOLIO_PATH` (line 57); replace the helper body at line 143-147 (`def load_portfolio(path: Path = PORTFOLIO_PATH) -> dict: ... return verified_load(str(path))`). The comment at line 1344 stays — it documents intent and the upstream loader call now reads PG.
- Modify: `src/xenon/reports/scenario_analysis.py` — replace `with open("data/portfolio.json") as f: portfolio = json.load(f)` at line 732.
- Test: `scripts/tests/test_reports.py` (create).

- [ ] **Step 11.1: Failing smoke tests**

```python
# scripts/tests/test_reports.py
"""Smoke tests: report modules read portfolio from Postgres."""
from __future__ import annotations

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot

PAYLOAD = {
    "positions": [{
        "ticker": "AAPL", "structure": "Stock", "expiry": "N/A",
        "contracts": 100, "market_price": 190, "avg_cost": 185,
        "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100,
                  "strike": 0, "avg_cost": 185, "market_price": 190}],
    }],
    "bankroll": 100_000,
    "account_summary": {"net_liquidation": 100_000},
}


def test_free_trade_analyzer_loads_from_pg(pg_clean):
    seed_portfolio_snapshot(PAYLOAD)
    from xenon.reports.free_trade_analyzer import load_portfolio
    assert load_portfolio()["positions"][0]["ticker"] == "AAPL"


def test_portfolio_performance_loads_from_pg(pg_clean):
    seed_portfolio_snapshot(PAYLOAD)
    from xenon.reports.portfolio_performance import load_portfolio
    assert load_portfolio()["positions"][0]["ticker"] == "AAPL"


def test_scenario_analysis_loads_from_pg(pg_clean):
    seed_portfolio_snapshot(PAYLOAD)
    from xenon.reports.scenario_analysis import load_portfolio
    assert load_portfolio()["positions"][0]["ticker"] == "AAPL"
```

If a report module doesn't already expose a top-level `load_portfolio()` helper, add one as part of step 11.2 — it's the natural seam for the migration anyway.

- [ ] **Step 11.2: Migrate each module**

Use this shared helper in every report module:

```python
def load_portfolio() -> dict:
    """Load latest IB portfolio payload from Postgres (replaces data/portfolio.json)."""
    from xenon.utils.portfolio_loader import load_portfolio_payload_sync

    return load_portfolio_payload_sync() or {}
```

Then update the inline reads at the lines listed in **Files** above:

- `free_trade_analyzer.py`: replace lines 324-331 with `data = load_portfolio()`. Remove the `verified_load` import if no longer used.
- `portfolio_performance.py`: replace the body of the helper at lines 143-147 with the snippet above. Drop the `path: Path = PORTFOLIO_PATH` parameter; audit `grep -n "load_portfolio(" src/xenon/reports/portfolio_performance.py` for callers passing `path=` and remove the kwarg.
- `scenario_analysis.py:732`: replace the `with open(...) as f: portfolio = json.load(f)` block with `portfolio = load_portfolio()` and add the helper above the use site.

Drop `PORTFOLIO_FILE` / `PORTFOLIO_PATH` constants once their last reference is gone.

- [ ] **Step 11.3: Run report tests**

Run: `uv run pytest scripts/tests/test_reports.py -v` (and any pre-existing per-report test file).

- [ ] **Step 11.4: Commit**

```bash
git add src/xenon/reports/ scripts/tests/
git commit -m "fix(reports): source portfolio from postgres in analyzers and scenarios"
```

---

## Task 12: Delete `data/portfolio.json` + add CI assertion

**Files:**

- Delete: `data/portfolio.json` (if present in repo or working tree)
- Create: `scripts/tests/test_portfolio_json_not_read.py`
- Modify: `docs/architecture/data-files.md` (note: file removed, runtime path is PG)
- Modify: `src/xenon/CLAUDE.md` "Legacy data files" section
- Modify: `docs/plans/2026-04-27-portfolio-postgres-read-path.md` (mark Phase 2 done)

- [ ] **Step 12.1: Delete the file**

```bash
git rm -f data/portfolio.json 2>/dev/null || rm -f data/portfolio.json
```

(May not exist — both paths are fine.)

- [ ] **Step 12.2: Add failing CI assertion**

```python
# scripts/tests/test_portfolio_json_not_read.py
"""Regression: ensure no source file under src/ or scripts/ references data/portfolio.json.

Phase 2 of the postgres-read-path migration deleted the file. If a future
PR re-introduces a reader, this test catches it before it hits master.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_PATTERNS = [
    # Doc/comment references are OK; only flag actual code reads.
    re.compile(rb"^\s*#"),
    re.compile(rb'"""'),
    re.compile(rb"'''"),
]


def test_no_source_reads_portfolio_json():
    out = subprocess.check_output(
        ["git", "grep", "-n", "portfolio.json",
         "--", "src/", "scripts/", ":^scripts/migrations/", ":^scripts/tests/"],
        cwd=REPO_ROOT,
    )
    offenders: list[str] = []
    for line in out.decode().splitlines():
        path, _, _content = line.partition(":")
        # Allow references inside the migration script itself (one-shot tool).
        if "scripts/migrations/migrate_to_postgres.py" in path:
            continue
        # Allow doc comments — flag actual code calls.
        if "open(" in line or "read_text" in line or "verified_load" in line or "json.load" in line:
            offenders.append(line)
    assert not offenders, "Source code still reads data/portfolio.json:\n" + "\n".join(offenders)
```

- [ ] **Step 12.3: Run and confirm green**

Run: `uv run pytest scripts/tests/test_portfolio_json_not_read.py -v`
Expected: PASS (no readers left).

If it fails, the failing line tells you which Task missed a site — go back and fix it before continuing.

- [ ] **Step 12.4: Update docs**

Edit `docs/architecture/data-files.md`: under the `data/portfolio.json` entry, replace the description with "Removed 2026-04-27. Portfolio payload now lives in `xenon.account_snapshots.payload` (jsonb). See `docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md`."

Edit `src/xenon/CLAUDE.md` "Legacy data files" section: append "`data/portfolio.json` removed 2026-04-27 — IB portfolio reads now go through `xenon.utils.portfolio_loader`."

Edit `docs/plans/2026-04-27-portfolio-postgres-read-path.md`: under Task 8 Phase-2 boundary, append `**Phase 2 done 2026-04-27** — see `docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md`.`

- [ ] **Step 12.5: Run full Python suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 12.6: Commit**

```bash
git add -A
git commit -m "chore(portfolio): delete data/portfolio.json — postgres is the source of truth"
```

- [ ] **Step 12.7: Push and open PR**

```bash
git push -u origin fix/portfolio-postgres-read-path-phase2
gh pr create --title "fix(portfolio): migrate all readers off data/portfolio.json (Phase 2)" \
  --body "$(cat <<'EOF'
## Summary

Phase 2 of the portfolio postgres read-path migration. PR #56 stopped writing
`data/portfolio.json` and migrated the FastAPI `/portfolio` endpoint; this PR
migrates the remaining 12 Python readers and deletes the file.

Safety-critical readers (preflight, naked-short audit, ib_reconcile,
incremental_sync, ib_sync's own self-read) source the payload via the new
`xenon.utils.portfolio_loader.load_portfolio_payload_sync(scope=...)` so
broker-account scope is preserved. Discovery readers (scanners, analyst
ratings, UW candidates, reports) use the scope-naive variant.

## Test plan

- [x] `uv run pytest` green
- [x] `scripts/tests/test_portfolio_json_not_read.py` enforces no future regressions
- [ ] Manual: `dev.sh live` → place a SELL order → preflight evaluates against the live PG snapshot, not stale JSON
- [ ] Manual: `xenon-ib-sync --sync` followed by `xenon-uw-analyze` → tickers come from PG
EOF
)"
```

---

## Closes when

- All tasks 1–12 ticked
- PR merged to master
- `data/portfolio.json` not in working tree
- `docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md` moves to `docs/plans/archive/`
- Memory entry `project_postgres_migration_read_side_gap.md` updated to "Phase 2 shipped — only Futu→PG migration remains"
