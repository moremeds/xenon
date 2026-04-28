# Postgres Migration Completion — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to execute task-by-task.
>
> **Companion doc:** `docs/plans/2026-04-28-postgres-migration-completion.md` holds the strategy + scope. This doc holds executable tasks.

**Goal:** Complete the file→Postgres migration end-to-end across the order-and-trade pipeline. Make PG the single runtime source of truth for submission, fill, trade ledger, blotter, journal, performance, and orders-list reads. Reduce `data/*.json` to one-shot import buffers and adapter-boundary inputs only.

**Architecture:** Submissions write `xenon.order_submissions`. IB events advance state via `xenon.order_events` (new `FILL` kind). `mark_terminal()` becomes the single transactional gate that also inserts into `xenon.trades` (the realized P&L ledger). All Next.js + FastAPI runtime read paths query PG. Flex Query becomes an optional audit overlay. Files persist only as legacy backfill input or external-adapter cache.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Alembic, Pydantic, ib_insync, Next.js App Router, Vitest, React Testing Library, Playwright/chrome-cdp, `uv run pytest`.

---

## Current State (verified 2026-04-28)

```
xenon.trades:                     1 row total (legacy_unknown)
xenon.order_submissions:          15 rows past 3 days
                                  states: UNKNOWN(7), PENDING(5), WORKING(2), REJECTED(1)
                                  no FILLED, no CANCELLED transitions
xenon.order_events:               11 events
                                  kinds: REHYDRATE_UNCERTAIN, MODIFY, CANCEL, PREFLIGHT_ACK_LIMIT
                                  no FILL kind exists in code
ib_execute.py:345:                inserts xenon.trades only on inline-watch path
orders_store.py:351 mark_terminal: state-only update; no trades insert
single_leg_rehydrate.py:185:      writes UNKNOWN when execs snapshot empty + positions changed
```

Hot stale-file readers still on master:

```
web/app/api/performance/route.ts:11        → data/portfolio.json
web/app/api/portfolio/route.ts:23          → data/trade_log.json (entry-date join)
web/app/api/orders/route.ts:18             → data/orders.json
web/app/api/orders/cancel/route.ts:42      → data/orders.json
web/app/api/orders/modify/route.ts:163,185,240 → data/orders.json
web/app/api/journal/route.ts:7             → data/trade_log.json
web/app/api/journal/sync/route.ts          → data/trade_log.json
web/app/api/pi/route.ts:91,346,372         → portfolio.json, trade_log.json
web/app/api/futu/portfolio/route.ts:9      → data/futu_portfolio.json (DEFERRED)
src/xenon/api/server.py:2552               → writes data/vcg.json (zombie)
src/xenon/api/server.py:2745               → writes data/gex.json (zombie)
```

## Workstream Sequence

```
Day 0       W2 (UX fix) — independent, ships immediately
Week 1      W1 (fill capture) — keystone
Week 2      W3 (blotter PG-first) + W4-orders-trio
Week 3      W4-performance + W4-portfolio-entry + W4-journal + W4-pi
Week 4      W5 (dual-write removal) + W6 (observability)
Ongoing     W7 (CLI cleanup)
Deferred    W4.9 (Futu) — ~2026-05-03 per existing memory
```

---

# WORKSTREAM W2 — Historical Trades UX Fix (Day 0)

**Branch:** `fix/blotter-empty-state`

## Task W2.1: Make `/blotter` Return 200 When Flex Unconfigured

**Files:**

- Modify: `src/xenon/api/server.py` (around `:2835` `blotter_sync()`)
- Modify: `src/xenon/trade_blotter/flex_query.py` (around `:436` setup-error path)
- Create: `scripts/tests/test_blotter_unconfigured.py`

**Step 1: Write failing test**

```python
# scripts/tests/test_blotter_unconfigured.py
import os
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IB_FLEX_QUERY_ID", raising=False)
    from xenon.api.server import app
    with TestClient(app) as c:
        yield c

def test_blotter_returns_200_with_configured_false_when_flex_creds_missing(client):
    resp = client.post("/blotter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False
    assert body["closed_trades"] == []
    assert body["open_trades"] == []
    assert "message" in body
```

Run: `uv run pytest scripts/tests/test_blotter_unconfigured.py -q` → FAIL (currently 502).

**Step 2: Add machine-readable exit code from `flex_query.py`**

In `src/xenon/trade_blotter/flex_query.py:436`, change the credentials-missing branch to also emit a JSON marker on stderr/stdout the FastAPI subprocess wrapper can detect:

```python
if not token or not query_id:
    print(json.dumps({"error": "FLEX_NOT_CONFIGURED", "missing": [...]}), file=sys.stderr)
    return 2  # distinct from generic error code 1
```

**Step 3: Detect and translate in `/blotter`**

```python
@app.post("/blotter")
async def blotter_sync():
    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)
    if not result.ok:
        # Distinguish "not configured" from real failures
        if "FLEX_NOT_CONFIGURED" in (result.error or ""):
            payload = {
                "configured": False,
                "closed_trades": [],
                "open_trades": [],
                "as_of": None,
                "message": "IB Flex Query not configured. Set IB_FLEX_TOKEN and IB_FLEX_QUERY_ID in .env. See `uv run python -m xenon.trade_blotter.flex_query --setup`.",
            }
            _write_cache(DATA_DIR / "blotter.json", payload)
            return payload
        raise HTTPException(status_code=502, detail=result.error)
    payload = {**result.data, "configured": True}
    _write_cache(DATA_DIR / "blotter.json", payload)
    return payload
```

**Step 4: Frontend renders friendly empty state**

Modify `web/components/WorkspaceSections.tsx::HistoricalTradesSection` (~line 3426):

```tsx
if (data?.configured === false) {
  return (
    <div className="section">
      <div className="section-header">...same header...</div>
      <div className="empty-state">
        <p>{data.message}</p>
        <p className="meta">
          IB Flex Query is optional — historical trades will populate from
          Postgres once the order pipeline records fills (W1).
        </p>
      </div>
    </div>
  );
}
```

Add a Vitest test in `web/tests/historical-trades-unconfigured.test.tsx` covering the new branch.

**Step 5: Update CLAUDE.md credentials table**

In `/Users/chenxi/projects/xenon/CLAUDE.md`, append to the `.env (root)` row:

```
... existing ..., IB_FLEX_TOKEN (optional), IB_FLEX_QUERY_ID (optional)
```

Add a one-liner: "Optional — when unset, Historical Trades panel shows empty state with setup hint instead of error."

**Step 6: Run green + verify**

```bash
uv run pytest scripts/tests/test_blotter_unconfigured.py scripts/tests/test_blotter*.py -q
cd web && npm test -- historical-trades-unconfigured
cd web && npx playwright test --grep "historical trades empty"
```

Open the dashboard locally with no Flex creds — verify the panel shows the empty state, not red 502.

**Step 7: Commit**

`fix(blotter): show empty state when IB Flex Query unconfigured`

**Acceptance for W2.1:**

- `/blotter` returns 200 with `configured: false` when Flex creds missing.
- UI shows actionable empty state, not red error.
- Vitest + pytest green; Playwright E2E green.

---

# WORKSTREAM W1 — Fill Capture (KEYSTONE; Week 1)

**Branch:** `feat/fill-capture-pipeline`

## Task W1.0: Diagnostic Probe — Why Are Orders Stuck in UNKNOWN?

**Files:**

- Create: `scripts/diagnostic/probe_unknown_orders.py`

**Step 1: Write probe script**

```python
"""Diagnose why xenon.order_submissions rows land in state=UNKNOWN.

Prints, for each UNKNOWN row in the last 24h:
- perm_id, ib_order_id, ticker, action, qty, submitted_at, updated_at
- Whether perm_id appears in current IB open orders snapshot
- Whether perm_id appears in current IB executions() snapshot
- Whether xenon.positions has changed since submitted_at
"""
# Read order_submissions where state='UNKNOWN' AND submitted_at > now()-24h
# Compare against ib_pool.acquire('data') -> reqAllOpenOrders + executions()
# Output: tabular report
```

Run: `uv run python scripts/diagnostic/probe_unknown_orders.py`

**Step 2: Decision tree**

Based on output, decide root cause:

| Symptom                                              | Root Cause                                       | Fix Site                                                                                                            |
| ---------------------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `executions()` returns empty for ALL perm_ids        | Gateway lost executions on restart               | Replay from IB Flex once daily (separate concern); short-term: stop writing UNKNOWN on positions-changed=True alone |
| `executions()` has data but rehydrate doesn't see it | Wrong pool client role / wrong session           | `single_leg_rehydrate.py:60-100`                                                                                    |
| `positions_changed=None` returned (check skipped)    | Position-change check wired to wrong source      | `single_leg_rehydrate.py:160-180`                                                                                   |
| `perm_id=None` (client-side 0) on row                | Race per memory `feedback_ib_insync_permid_race` | `_orders_place_from_body` — poll openOrder before persisting perm_id                                                |

**Step 3: Document findings**

Append a `## Diagnostic Findings 2026-04-28` section to this plan. The findings determine which subset of W1.3 tasks ship.

**Acceptance for W1.0:** Concrete answer to "why UNKNOWN" with file/line citations. No code changes yet.

## Task W1.1: Add `FILL` Event Kind + Insert Into `xenon.trades` On Finalize

**Files:**

- Modify: `src/xenon/execution/orders_store.py` (`mark_terminal()` ~line 351)
- Modify: `src/xenon/db/queries/trades.py` (extend with `record_finalized_trade()`)
- Modify: `src/xenon/db/schema.py` (no schema migration; `kind` is text)
- Create: `scripts/tests/test_mark_terminal_writes_trades.py`

**Step 1: Write failing test**

```python
def test_mark_terminal_writes_trades_row_and_fill_event(db_session, sample_submission):
    # Given: an order_submissions row in WORKING state
    # When: mark_terminal(sub_id, state="FILLED", filled_qty=100, avg_fill_price=Decimal("500.25"))
    # Then:
    #   - order_submissions.state == "FILLED"
    #   - order_submissions.filled_qty == 100
    #   - order_submissions.avg_fill_price == Decimal("500.25")
    #   - order_events row with kind="FILL" exists
    #   - xenon.trades has a new row with:
    #       ticker=sample_submission.ticker
    #       action=sample_submission.action
    #       quantity=100
    #       entry_cost=500.25 * 100 (or matching cost basis)
    #       broker, account_env, broker_account scoped from submission
```

Run red: `uv run pytest scripts/tests/test_mark_terminal_writes_trades.py -q` → FAIL (no trades row written).

**Step 2: Implement transactional finalize**

Modify `orders_store.mark_terminal()`:

```python
def mark_terminal(
    submission_id: str,
    *,
    state: Literal["FILLED", "REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED"],
    filled_qty: int = 0,
    avg_fill_price: Decimal | None = None,
    reason_code: str | None = None,
    fill_detail: dict | None = None,
) -> None:
    with engine.begin() as conn:
        sub = conn.execute(
            select(order_submissions).where(order_submissions.c.submission_id == submission_id)
        ).one_or_none()
        if sub is None:
            raise ValueError(f"submission {submission_id} not found")

        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id)
            .values(
                state=state,
                filled_qty=filled_qty,
                avg_fill_price=avg_fill_price,
                reason_code=reason_code,
                updated_at=func.now(),
            )
        )

        if state == "FILLED" or state == "PARTIALLY_FILLED":
            conn.execute(
                insert(order_events).values(
                    submission_id=submission_id,
                    kind="FILL",
                    detail={
                        "filled_qty": filled_qty,
                        "avg_fill_price": str(avg_fill_price) if avg_fill_price else None,
                        **(fill_detail or {}),
                    },
                )
            )
            # P1 decision: trade-grain = 1 row per round-trip
            # On FILLED, write trades row with entry side; close happens on the closing fill
            _record_finalized_trade(conn, sub, filled_qty, avg_fill_price, fill_detail)
        else:
            conn.execute(
                insert(order_events).values(
                    submission_id=submission_id,
                    kind=state,  # CANCELLED, REJECTED, FAILED
                    detail={"reason_code": reason_code} if reason_code else {},
                )
            )

def _record_finalized_trade(conn, sub, filled_qty, avg_fill_price, fill_detail):
    """Insert or update xenon.trades for this fill.

    P1 (logical position grain): if an open trades row exists for
    (broker, account_env, broker_account, ticker) where this fill closes the
    position, update closed_at + exit_cost + realized_pnl. Otherwise insert
    a new open row.
    """
    # See db/queries/trades.py for the open-trade lookup logic.
    ...
```

**Step 3: Combo wizard finalization**

For combo orders, write **one** `xenon.trades` row per combo (P2 decision), with leg detail in `metadata.legs[]`. Modify `combo_wizard/rehydrate.py:144-170` so the all-legs-FILLED path calls `mark_terminal` once with combo-level structure.

**Step 4: Backfill `xenon.trades.structure` and `decision`**

Add a small helper that computes `structure` from the order body (single leg → `"Stock" | "Long Call" | ...`; combo → `"Vertical Call Spread" | ...`) using `docs/trading/options-structures.json` as the authoritative table.

**Step 5: Run green + commit**

```bash
uv run pytest scripts/tests/test_mark_terminal_writes_trades.py scripts/tests/test_orders_store.py -q
```

Commit: `feat(orders): write xenon.trades + FILL event on mark_terminal`

**Acceptance for W1.1:**

- `mark_terminal(state="FILLED")` writes both `order_events.kind=FILL` and a `xenon.trades` row in one transaction.
- Combo wizard fills produce 1 trades row with full leg detail in `metadata`.
- Existing single-leg paper-fill paper-smoke ends with both PG rows present.

## Task W1.2: Wire Live Fill Events Into `mark_terminal`

**Files:**

- Modify: `src/xenon/execution/single_leg_rehydrate.py` (~`:140-160` FILLED branch)
- Modify: `src/xenon/execution/ib_execute.py` (~`:178-345` inline-fill path)
- Modify: `src/xenon/execution/combo_wizard/rehydrate.py` (~`:144-170`)
- Create: `scripts/tests/test_rehydrate_writes_fill_event.py`

**Step 1: Write failing integration test**

```python
def test_async_rehydrate_writes_trades_row_on_fill(db_session, monkeypatch):
    # Stub ib_pool to return:
    #   - open_orders: empty (order is gone)
    #   - executions: {perm_id: {"shares": 100, "avg_price": Decimal("500.25")}}
    #   - positions: changed (long 100 SPY now)
    # Call rehydrate.run_once()
    # Assert:
    #   - order_submissions.state == "FILLED"
    #   - xenon.trades has one new row for SPY
    #   - order_events has kind="FILL"
```

**Step 2: Implement**

In `single_leg_rehydrate.py`, the existing FILLED branch already updates state — add the `mark_terminal()` call instead of direct UPDATE so the same transactional pathway writes `trades` + `FILL` event.

In `ib_execute.py:345`, replace the bare `insert(trades)` with `mark_terminal()` to deduplicate logic.

In `combo_wizard/rehydrate.py`, the `FILLED` branch should call `mark_terminal` with combo-level body (P2: one trades row per combo).

**Step 3: Eliminate the inline-only path**

Old code in `ib_execute.py:345` writes `trades` directly without going through `mark_terminal`. Remove that direct insert; route through `mark_terminal` instead. This guarantees rehydrate path and inline path produce identical PG state.

**Step 4: Run green + paper smoke**

```bash
uv run pytest scripts/tests/test_rehydrate_writes_fill_event.py -q
# Paper-smoke against XENON_TRADING_MODE=paper:
uv run python scripts/diagnostic/probe_unknown_orders.py  # baseline
# place a marketable paper order, wait for fill, run rehydrate
uv run xenon-ib-orders --sync --port 4002
psql -h localhost -U xenon_app xenon_db -c "SELECT state, filled_qty, avg_fill_price FROM xenon.order_submissions ORDER BY submitted_at DESC LIMIT 3"
psql -h localhost -U xenon_app xenon_db -c "SELECT ticker, action, quantity, entry_cost FROM xenon.trades ORDER BY id DESC LIMIT 3"
```

Both should show the new fill.

**Step 5: Commit**

`feat(rehydrate): route fill outcomes through mark_terminal; close UNKNOWN gap`

**Acceptance for W1.2:**

- New paper fills land in `xenon.trades` regardless of inline vs async path.
- Existing rows stuck in UNKNOWN can be re-reconciled by replaying rehydrate.

## Task W1.3: Fix Rehydrate Execution-Snapshot Lookup (Conditional on W1.0 Findings)

**Files:**

- Modify: `src/xenon/execution/single_leg_rehydrate.py` (depends on W1.0 root cause)

**Step 1: Apply fix per W1.0 decision tree**

Based on probe output, implement one of:

**Variant A — Wrong pool role:** rehydrate uses the stale execution pool role instead of `pool.acquire('data')` for executions snapshot. Fix the role parameter.

**Variant B — Gateway loses executions:** add a daily replay from IB Flex Query that retroactively transitions stale UNKNOWN rows to FILLED based on Flex executions. New script `src/xenon/execution/flex_replay.py`.

**Variant C — `positions_changed=None` defaults to True:** narrow the default — when positions check is skipped, leave state unchanged instead of forcing UNKNOWN. Update `_reconcile_from_three_sources`.

**Variant D — perm_id race:** poll `openOrder` ack with timeout before persisting `perm_id` to `order_submissions`, per existing memory `feedback_ib_insync_permid_race`.

**Step 2: Backfill UNKNOWN rows**

One-shot script `scripts/migrations/_2026_04_28_replay_unknown_orders.py`:

```python
"""For every order_submissions row in state=UNKNOWN, attempt re-reconcile via
current IB executions snapshot. Promote to FILLED/CANCELLED/REJECTED where
deterministic; leave UNKNOWN with a logged reason where ambiguous."""
```

**Step 3: Tests**

`scripts/tests/test_unknown_replay.py` covers all four variants above.

**Step 4: Commit**

`fix(rehydrate): close UNKNOWN cluster — <variant>`

**Acceptance for W1.3:**

- Probe output (W1.0) re-run shows zero new UNKNOWN rows over a paper-trading session.
- Backfill migration successfully resolves the existing 7 UNKNOWN rows from your DB.

## Task W1.4: Backfill `xenon.trades` From Legacy `trade_log.json`

**Files:**

- Create: `scripts/migrations/_2026_04_28_backfill_trades_from_trade_log.py`
- Create: `scripts/tests/test_backfill_trades_from_trade_log.py`

**Step 1: Read all legacy `data/trade_log.json` entries**

```python
"""For each entry in trade_log.json, INSERT INTO xenon.trades:
- ticker, structure, action, quantity, entry_cost, exit_cost, realized_pnl
- opened_at, closed_at
- decision (from journal), edge (from journal)
- broker='IB', account_env='legacy_unknown', broker_account='legacy_unknown'
- metadata = original entry as JSONB

Idempotent: derive a stable hash key from (ticker, opened_at, quantity, decision)
and skip duplicates."""
```

**Step 2: Tests**

- Idempotency: running the script twice produces the same row count.
- Round-trip: opened+closed entries get one row each with full P&L.
- Open positions: opened-but-not-closed entries get rows with `closed_at IS NULL`.

**Step 3: Run + verify**

```bash
uv run python scripts/migrations/_2026_04_28_backfill_trades_from_trade_log.py
psql -h localhost -U xenon_app xenon_db -c "SELECT COUNT(*), MIN(opened_at), MAX(closed_at) FROM xenon.trades"
```

Compare row count against `jq 'length' data/trade_log.json`.

**Step 4: Commit**

`feat(migration): backfill xenon.trades from legacy trade_log.json`

**Acceptance for W1.4:** every legacy fill in `trade_log.json` has a corresponding row in `xenon.trades`. Re-running migration is a no-op.

## Task W1.5: Add Combo-Level Trade Recording

**Files:**

- Modify: `src/xenon/execution/combo_wizard/rehydrate.py:144-170`
- Modify: `src/xenon/db/queries/trades.py` (helper `_record_combo_trade`)
- Create: `scripts/tests/test_combo_trade_recording.py`

**Step 1: Decision: P2 — one trades row per combo**

Combo trade row shape:

- `structure`: e.g. `"Vertical Call Spread"`, `"Iron Condor"` (from `docs/trading/options-structures.json`)
- `quantity`: combo unit count
- `entry_cost`: sum across legs (signed by direction)
- `metadata.legs`: full leg breakdown

**Step 2: Implementation**

When `combo_wizard/rehydrate.py` reaches `to_state="FILLED"`, call `mark_terminal` with a combo-shaped body. The trade-recording helper detects `body.type == "combo"` and writes one row.

**Step 3: Tests**

- Vertical spread fills → 1 trades row with `structure="Vertical Call Spread"`.
- Iron condor partially filled → 1 trades row with `state="PARTIALLY_FILLED"` semantics.
- Closing leg fills update `closed_at` + `exit_cost` + `realized_pnl`.

**Step 4: Commit**

`feat(combo): record combo wizard fills as single trades rows`

**Acceptance for W1.5:** combo fills produce structurally-correct ledger rows usable by blotter/journal/performance.

## Task W1.6: Verification Gate Before W3 / W4 Begin

**Step 1: Paper-smoke matrix**

Run from clean PG (truncate `xenon.trades` to test):

1. Stock BUY → fill → row in trades ✓
2. Stock SELL closing → trades row updates with exit_cost + realized_pnl ✓
3. Single-leg option BUY → fill → row in trades ✓
4. Combo (vertical spread) BUY → fill → 1 row in trades ✓
5. REJECTED order → `order_events.kind='REJECTED'` only, no trades row ✓
6. CANCELLED order → `order_events.kind='CANCELLED'` only, no trades row ✓
7. PARTIALLY_FILLED → trades row with partial qty ✓

**Step 2: Dual-source sanity**

If Flex configured: compare PG trades vs Flex trades for the same day. Flag any divergence > 0 contracts. Document tolerance for review.

**Step 3: Sign-off**

Update memory `project_postgres_migration_read_side_gap` to reflect W1 complete. Now W3 + W4 unblocked.

---

# WORKSTREAM W3 — Blotter PG-First (Week 2)

**Branch:** `feat/blotter-pg-first`

## Task W3.1: PG Blotter Query Module

**Files:**

- Create: `src/xenon/db/queries/blotter.py`
- Create: `scripts/tests/test_blotter_query.py`

**Step 1: Write failing test**

```python
def test_blotter_pg_query_returns_closed_and_open_trades(db_session, sample_trades):
    from xenon.db.queries.blotter import fetch_blotter_pg
    result = fetch_blotter_pg(broker="IB", account_env="paper", broker_account="DUQ378889", days=30)
    assert "closed_trades" in result
    assert "open_trades" in result
    assert all(t["is_closed"] for t in result["closed_trades"])
    assert all(not t["is_closed"] for t in result["open_trades"])
    assert "as_of" in result
```

**Step 2: Implement**

```python
def fetch_blotter_pg(*, broker, account_env, broker_account, days=30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(trades)
        .where(trades.c.broker == broker)
        .where(trades.c.account_env == account_env)
        .where(trades.c.broker_account == broker_account)
        .where(or_(trades.c.opened_at >= cutoff, trades.c.closed_at >= cutoff))
        .order_by(trades.c.opened_at.desc())
    )
    rows = engine.connect().execute(stmt).all()
    closed, open_ = [], []
    for row in rows:
        item = _row_to_blotter_trade(row)  # shape matches existing Flex output
        (closed if row.closed_at else open_).append(item)
    return {
        "closed_trades": closed,
        "open_trades": open_,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "configured": True,
        "source": "postgres",
    }
```

`_row_to_blotter_trade()` produces the existing `BlotterTrade` shape consumed by `web/components/WorkspaceSections.tsx`:

```python
{
    "symbol": row.ticker,
    "contract_desc": row.metadata.get("contract_desc") or row.ticker,
    "sec_type": row.metadata.get("sec_type") or "STK",
    "is_closed": row.closed_at is not None,
    "executions": [...],  # synthesized from order_events.kind=FILL for this submission_id
    "structure": row.structure,
    "realized_pnl": float(row.realized_pnl or 0),
    ...
}
```

**Step 3: Tests + commit**

`feat(blotter): postgres query module`

## Task W3.2: Switch `/blotter` Route To PG-First

**Files:**

- Modify: `src/xenon/api/server.py:2835` `blotter_sync()`
- Modify: `scripts/tests/test_blotter_unconfigured.py` (extend)

**Step 1: Update test to assert PG primary**

```python
def test_blotter_reads_pg_when_trades_exist(client, db_session, sample_trades):
    resp = client.post("/blotter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["configured"] is True  # PG presence counts as configured
    assert len(body["closed_trades"]) >= 1
```

**Step 2: Implementation**

```python
@app.post("/blotter")
async def blotter_sync(scope: AccountScope = Depends(get_account_scope)):
    pg_result = fetch_blotter_pg(
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
        days=30,
    )

    has_pg_data = bool(pg_result["closed_trades"] or pg_result["open_trades"])
    flex_configured = bool(os.environ.get("IB_FLEX_TOKEN") and os.environ.get("IB_FLEX_QUERY_ID"))

    if has_pg_data and not flex_configured:
        _write_cache(DATA_DIR / "blotter.json", pg_result)
        return pg_result

    if not flex_configured:
        # PG empty + no Flex → empty state with hint
        empty = {
            "configured": False,
            "closed_trades": [],
            "open_trades": [],
            "as_of": None,
            "source": "none",
            "message": "No trades in Postgres yet. Set IB_FLEX_TOKEN/IB_FLEX_QUERY_ID for legacy import, or trade through Xenon to populate PG.",
        }
        return empty

    # Flex configured → run as overlay/fallback
    flex_result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)
    if not flex_result.ok:
        # Even on Flex failure, return PG result if any
        if has_pg_data:
            return {**pg_result, "flex_error": flex_result.error}
        raise HTTPException(status_code=502, detail=flex_result.error)

    merged = _merge_pg_and_flex(pg_result, flex_result.data)
    _write_cache(DATA_DIR / "blotter.json", merged)
    return merged
```

**Step 3: Implement merge**

`_merge_pg_and_flex()`: union by `perm_id` (when present) else by `(ticker, opened_at, quantity)`. Flag rows where Flex P&L diverges from PG by > $0.01 with `divergence: true` in metadata. Source field becomes `"postgres+flex"`.

**Step 4: Frontend source indicator**

Modify `HistoricalTradesSection` to render a small pill in the header:

```tsx
{
  data?.source === "postgres" && <span className="pill pg">PG</span>;
}
{
  data?.source === "postgres+flex" && (
    <span className="pill pg-flex">PG+Flex</span>
  );
}
{
  data?.source === "flex" && <span className="pill flex">Flex</span>;
}
```

**Step 5: Run + commit**

`feat(blotter): postgres-first read with optional Flex overlay`

## Task W3.3: Blotter E2E

**Files:**

- Create: `web/tests/blotter-pg-first.test.ts`
- Modify: `web/playwright.config.ts` (if needed)

**Step 1: Vitest route-level test**

Mock FastAPI to return a PG-source response; assert UI renders trades with `Source: PG` pill, no setup error.

**Step 2: Playwright E2E**

Boot FastAPI in `XENON_TRADING_MODE=paper`, place a paper order, wait for fill (or stub via `XENON_API_TEST_MODE`), refresh blotter panel, assert `0 TRADES → N TRADES` transition with PG source.

**Step 3: Commit**

`test(blotter): pg-first E2E + Vitest coverage`

**Acceptance for W3:** `/blotter` returns PG data without Flex, falls back gracefully when PG empty + Flex unset, merges divergent sources when both present.

---

# WORKSTREAM W4 — Hot Read-Side Migration (Weeks 2–3)

## Task W4.1: `/api/performance` → PG (No W1 dependency)

**Branch:** `fix/performance-pg-read`

**Files:**

- Modify: `web/app/api/performance/route.ts:11`
- Create: `web/tests/performance-pg-read.test.ts`
- Create: `scripts/tests/test_performance_json_not_read.py`

**Step 1: Write failing test (CI guard)**

Mirror existing `scripts/tests/test_vcg_json_not_read.py`:

```python
def test_performance_route_does_not_read_portfolio_json(monkeypatch):
    """Stub readFile to throw; performance route should still work via FastAPI."""
    # ...
```

**Step 2: Replace `data/portfolio.json` read with FastAPI proxy**

```ts
// web/app/api/performance/route.ts
const portfolioResult = await xenonFetch("/portfolio", { method: "GET", timeout: 10_000 });
if (!portfolioResult || /* error */) { ... }
// no readFile of data/portfolio.json
```

**Step 3: Vitest + Playwright E2E**

`web/tests/performance-pg-read.test.ts`: mock `xenonFetch` to return scoped portfolio; assert UI renders correctly.

E2E: open Performance section in browser; verify NAV chart renders.

**Step 4: Commit**

`fix(performance): read from postgres, not stale data/portfolio.json`

## Task W4.2: `/api/portfolio` Entry-Date Join → PG (Depends W1)

**Branch:** `fix/portfolio-entry-date-pg`

**Files:**

- Modify: `web/app/api/portfolio/route.ts:23-28` (`loadTradeLogDates`)

**Step 1: Replace function**

```ts
async function loadTradeLogDates(
  scope: AccountScope,
): Promise<Record<string, string>> {
  // Query xenon.trades.opened_at grouped by ticker (earliest open)
  const result = await xenonFetch<Record<string, string>>(
    `/trades/entry-dates?broker=${scope.broker}&account_env=${scope.account_env}&broker_account=${scope.broker_account}`,
    { method: "GET", timeout: 10_000 },
  );
  return result || {};
}
```

**Step 2: Add new FastAPI endpoint**

```python
@app.get("/trades/entry-dates")
async def trades_entry_dates(scope: AccountScope = Depends(get_account_scope)):
    stmt = (
        select(trades.c.ticker, func.min(trades.c.opened_at).label("first_open"))
        .where(trades.c.broker == scope.broker)
        .where(trades.c.account_env == scope.account_env)
        .where(trades.c.broker_account == scope.broker_account)
        .group_by(trades.c.ticker)
    )
    rows = engine.connect().execute(stmt).all()
    return {row.ticker: row.first_open.isoformat() for row in rows}
```

**Step 3: Tests + commit**

CI guard test: `test_portfolio_route_does_not_read_trade_log_json`.

`fix(portfolio): entry dates from xenon.trades, not stale trade_log.json`

## Task W4.3: `/api/orders` List → PG (No W1 dependency)

**Branch:** `fix/orders-list-pg-read`

**Files:**

- Modify: `web/app/api/orders/route.ts:18`
- Create: `scripts/tests/test_orders_json_not_read.py`

**Step 1: Write failing CI guard test**

**Step 2: Replace with FastAPI proxy**

```ts
const result = await xenonFetch("/orders", { method: "GET", timeout: 10_000 });
```

**Step 3: New FastAPI `/orders` endpoint** (or reuse existing if present)

```python
@app.get("/orders")
async def orders_list(scope: AccountScope = Depends(get_account_scope), limit: int = 200):
    """Return order_submissions list scoped by account."""
    # Active states first, then recent finalized
```

**Step 4: Commit**

`fix(orders): list from xenon.order_submissions, not stale data/orders.json`

## Task W4.4: `/api/orders/cancel` → PG

**Branch:** `fix/orders-cancel-pg-read`

**Files:**

- Modify: `web/app/api/orders/cancel/route.ts:42`

**Step 1: Replace `readDataFile("data/orders.json")` with FastAPI lookup**

The cancel route currently looks up the order in `data/orders.json` to get its `ib_order_id`. Replace with `GET /orders/by-id/{submission_id}` or include the relevant fields in the cancel request payload from the UI.

**Step 2: Tests + commit**

`fix(orders/cancel): look up via PG, not stale orders.json`

## Task W4.5: `/api/orders/modify` → PG

**Branch:** `fix/orders-modify-pg-read`

**Files:**

- Modify: `web/app/api/orders/modify/route.ts:163,185,240`

**Step 1: Replace all 3 read sites**

Same pattern as W4.4 — three call sites all do the same `readDataFile` lookup. Centralize in a helper.

**Step 2: Tests + commit**

`fix(orders/modify): look up via PG across all 3 call sites`

## Task W4.6: `/api/journal` → PG (Depends W1)

**Branch:** `feat/journal-pg`

**Files:**

- Create: `src/xenon/db/schema.py` add `journal_entries` table (P3 decision: separate table)
- Create: Alembic migration `xxxx_add_journal_entries.py`
- Modify: `web/app/api/journal/route.ts:7`
- Create: FastAPI `/journal` endpoint
- Create: `scripts/migrations/_2026_04_28_backfill_journal_from_trade_log.py`

**Step 1: Define `journal_entries` table**

```python
journal_entries = Table(
    "journal_entries",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("trade_id", BigInteger, ForeignKey(f"{XENON_SCHEMA}.trades.id"), nullable=True),
    Column("ticker", Text, nullable=False),
    Column("decision", Text),  # "IB_AUTO_IMPORT", "MANUAL", "POST_MORTEM", ...
    Column("note", Text),
    Column("attachments", JSONB),  # screenshots, links
    Column("authored_by", Text),
    Column("authored_at", TIMESTAMP(tz=True), nullable=False, server_default=tz_now),
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_journal_broker"),
    Index("ix_journal_ticker_at", "ticker", "authored_at"),
)
```

**Step 2: Alembic migration**

```bash
uv run alembic revision -m "add journal_entries table"
# fill out upgrade/downgrade with create_table / drop_table
```

**Step 3: Backfill migration**

`_2026_04_28_backfill_journal_from_trade_log.py`: read every entry in `data/trade_log.json`, insert as journal row with `decision="LEGACY_IMPORT"`. Idempotent via stable hash key.

**Step 4: FastAPI endpoint**

```python
@app.get("/journal")
async def journal_list(scope: AccountScope = Depends(get_account_scope), days: int = 90):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(journal_entries).where(...).order_by(journal_entries.c.authored_at.desc())
    return [_row_to_journal_entry(r) for r in engine.connect().execute(stmt)]

@app.post("/journal")
async def journal_create(body: dict, scope: AccountScope = Depends(get_account_scope)):
    # Create entry; require trade_id OR ticker minimum
```

**Step 5: Replace Next route**

```ts
// web/app/api/journal/route.ts
const result = await xenonFetch("/journal", { method: "GET", timeout: 10_000 });
```

**Step 6: Tests + commit**

CI guard test, Vitest, Playwright.

`feat(journal): postgres-backed journal_entries table; migrate read path`

## Task W4.7: `/api/journal/sync` → PG-Native Auto-Import

**Branch:** `feat/journal-sync-pg`

**Files:**

- Modify: `web/app/api/journal/sync/route.ts`
- Modify: `web/lib/journalSync.ts:162-170`

**Step 1: Q3 decision — replace `IB_AUTO_IMPORT` with PG event listener**

When a `xenon.trades` row closes after aggregation (W1), an `events.outbox` row on channel `trade.closed` fires. A FastAPI background task converts this to a `journal_entries` row with `decision="IB_AUTO_IMPORT"`.

This eliminates the need for the periodic sync route entirely — change `/api/journal/sync` to a no-op that returns the count of pending PG outbox rows.

**Step 2: Tests + commit**

`refactor(journal): replace periodic sync with PG outbox listener`

## Task W4.8: `/api/pi` → PG (Depends W1)

**Branch:** `fix/pi-pg-read`

**Files:**

- Modify: `web/app/api/pi/route.ts:91,346,372` (3 read sites)

**Step 1: Replace `portfolio.json` reads with `/portfolio` proxy**

**Step 2: Replace `trade_log.json` reads with `/journal` or `/trades` proxy**

**Step 3: Tests + commit**

`fix(pi): postgres-backed reads for private investment dashboard`

## Task W4.9: `/api/futu/portfolio` (DEFERRED ~2026-05-03)

Per existing memory `project_postgres_migration_read_side_gap` and `docs/plans/2026-04-27-futu-postgres-migration-followup.md`. Out of scope for this plan; tracked separately.

---

# WORKSTREAM W5 — Dual-Write Removal (Week 4)

**Branch:** `chore/remove-dual-writes`

## Task W5.1: Drop `data/gex.json` Write

**Files:**

- Modify: `src/xenon/api/server.py:2734-2745`

**Step 1: Confirm reader migration**

`grep -rn "gex.json" web/ src/` after W4 — should only show share-card generators (W7) and tests.

**Step 2: Remove `_write_cache(DATA_DIR / "gex.json", ...)`**

PG `gex_snapshots` is already authoritative.

**Step 3: Update share-card generator (W7.10) if needed**

`src/xenon/shares/generate_gex_share.py:30` reads `gex.json` — either migrate to PG or accept as legacy.

**Step 4: Commit**

`chore: drop data/gex.json dual-write (PG gex_snapshots authoritative)`

## Task W5.2: Drop `data/vcg.json` Write

**Files:**

- Modify: `src/xenon/api/server.py:2552`

Mirror W5.1. PG `vcg_series` already authoritative.

## Task W5.3: Drop `data/orders.json` Write

**Files:**

- Modify: wherever the writer lives (likely `xenon-ib-orders --sync`)

Confirm W4.3/4.4/4.5 readers gone first.

## Task W5.4: Decision — Scan Caches

**Files:**

- `src/xenon/api/server.py:1289,1302,2456,2841,2856`

**Q4 decision:** keep TTL-bounded files OR delete entirely?

Recommendation: delete `scanner.json`, `discover.json`, `cri.json`, `blotter.json`, `performance.json` writes. Replace with PG-backed read from `scan_results` / `cri_series` / etc. with explicit TTL in query (`WHERE created_at > now() - interval '5 minutes'`).

**Step 1: Per-file decision in this PR.**

**Step 2: Commit**

`chore: scan caches now PG-backed with TTL queries`

---

# WORKSTREAM W6 — Snapshot Freshness + Observability (Week 4)

**Branch:** `feat/snapshot-freshness-observability`

## Task W6.1: Snapshot Staleness Detection

**Files:**

- Modify: `src/xenon/api/server.py:1466` `_load_portfolio_view_sync`
- Modify: `src/xenon/execution/preflight.py` (add `PORTFOLIO_SNAPSHOT_STALE` reason code)

**Step 1: Return `(view, snapshot_at)`**

```python
def _load_portfolio_view_sync() -> tuple[PortfolioView | None, datetime | None]:
    ...
    return PortfolioView.model_validate(...), row.snapshot_at
```

**Step 2: Add staleness check in preflight**

```python
STALE_OPEN_S = int(os.environ.get("XENON_PORTFOLIO_SNAPSHOT_STALE_S", "300"))
STALE_CLOSED_S = int(os.environ.get("XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S", "1800"))

if snapshot_at:
    threshold = STALE_OPEN_S if market_open() else STALE_CLOSED_S
    if (now - snapshot_at).total_seconds() > threshold:
        return Verdict(accept=False, reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_STALE, ...)
```

**Step 3: Tests + commit**

## Task W6.2: `/health` Snapshotter Heartbeat

**Files:**

- Modify: `src/xenon/api/server.py` `/health` endpoint

**Step 1: Add metric**

```python
"snapshotter": {
    "last_write_at": <max(account_snapshots.snapshot_at)>,
    "stale_seconds": <now - last_write_at>,
}
```

**Step 2: Commit**

## Task W6.3: UNKNOWN Counter Alarm

**Files:**

- Modify: `src/xenon/api/server.py` `/health` endpoint

**Step 1: Add metric**

```python
"order_submissions": {
    "unknown_count": <COUNT(state='UNKNOWN' AND submitted_at > now()-1h)>,
    "alarm": <unknown_count > 5>,
}
```

**Step 2: Commit**

`feat(observability): surface UNKNOWN order count + snapshot freshness on /health`

---

# WORKSTREAM W7 — CLI Cleanup (Ongoing, Opportunistic)

**Branch (per task):** `chore/<file-being-cleaned>`

| Task  | File                                                  | Action                                                            | Effort  |
| ----- | ----------------------------------------------------- | ----------------------------------------------------------------- | ------- |
| W7.1  | `src/xenon/scanners/trend/cli.py:850`                 | **DELETE** the entire `trend/` subtree (deprecated per CLAUDE.md) | 30min   |
| W7.2  | `src/xenon/utils/incremental_sync.py`                 | Audit for callers — likely dead — delete                          | 1h      |
| W7.3  | `src/xenon/scanners/repair_cri_rvol_cache.py`         | One-shot script post-W5 — delete                                  | 30min   |
| W7.4  | `src/xenon/reports/portfolio_report.py:436`           | Migrate `load_trade_log` to PG (`xenon.trades` query)             | 2h      |
| W7.5  | `src/xenon/reports/free_trade_analyzer.py:34`         | Migrate to PG read                                                | 1h      |
| W7.6  | `src/xenon/reports/scenario_analysis.py:732`          | Migrate `data/portfolio.json` read to `account_snapshots.payload` | 1h      |
| W7.7  | `src/xenon/execution/naked_short_audit.py:300-302`    | Migrate to PG read                                                | 2h      |
| W7.8  | `src/xenon/execution/ib_reconcile.py:280`             | Refactor — becomes append-only writer to `xenon.trades` after W1  | 1d      |
| W7.9  | `src/xenon/fetchers/fetch_analyst_ratings.py:55-57`   | Migrate `PORTFOLIO_FILE` read to PG                               | 1h      |
| W7.10 | `src/xenon/shares/generate_{gex,vcg,regime}_share.py` | Migrate cache reads to PG                                         | 2h each |

Each task is a single-file PR. Mostly 1–2h of work.

---

# Cross-cutting Verification Tasks

## Task V.1: Add CI Guard Tests Per Migrated Route

For each migrated route in W4, add a `scripts/tests/test_<route>_json_not_read.py` mirroring the existing `test_vcg_json_not_read.py` pattern. CI fails if a runtime route reads a stale JSON file.

## Task V.2: Paper Smoke Matrix

Before each PR merge, run from `XENON_TRADING_MODE=paper`:

```bash
# 1. Boot fresh
./scripts/cloud.sh
# 2. Verify health
curl -s http://localhost:8321/health | jq
# 3. Run the affected matrix per workstream
```

Document results in PR body.

## Task V.3: Browser E2E Per UI Change

Per CLAUDE.md ⛔ rule 2 — every UI change must be visually confirmed.

```bash
# chrome-cdp preferred:
chrome-cdp http://localhost:3000 --screenshot
# or Playwright:
cd web && npx playwright test --grep "<feature>"
```

## Task V.4: Cross-Source Divergence Check (post-W3)

Nightly job comparing `xenon.trades` vs Flex output for the same day. Tolerance per P5 decision. Surface divergence count in `/health`.

---

# Non-Goals

- Migrating `data/futu_portfolio.json` (deferred per existing memory + plan).
- Deleting Flex Query support entirely — kept as optional audit overlay.
- Migrating `data/watchlist.json` (config file).
- Migrating `data/trend_scan.json` reads (W7.1 deletes producer entirely).
- Refactoring report scripts that aren't on hot user-facing paths.
- Schema changes to `xenon.trades` shape (P1 keeps current grain).

# Acceptance Criteria

- [ ] `xenon.trades` populated for every IB fill (paper + live), regardless of inline vs async path.
- [ ] Zero `state=UNKNOWN` rows older than 1 hour during normal operation.
- [ ] `/blotter` returns 200 + `closed_trades`/`open_trades` from PG without `IB_FLEX_TOKEN`.
- [ ] No `web/app/api/**` runtime route reads `data/*.json`.
- [ ] No FastAPI route writes `data/gex.json`, `data/vcg.json`, `data/orders.json`.
- [ ] CI guard tests prove stale-JSON-not-read for every migrated route.
- [ ] `/health` surfaces snapshot freshness + UNKNOWN count.
- [ ] Nightly PG↔Flex divergence check runs and posts to `/health`.
- [ ] CLAUDE.md credentials table includes optional `IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID`.
- [ ] Paper-smoke matrix passes for every workstream PR.
- [ ] Browser E2E confirms UI rendering for every UI change.

---

# Branching + PR Strategy

```
master ── PR #61 (shipped)
  │
  ├── fix/blotter-empty-state          (W2 — 30min, can ship parallel)
  │
  ├── feat/fill-capture-pipeline       (W1 — keystone, ~5d, single PR with sub-tasks)
  │   │
  │   ├── feat/blotter-pg-first        (W3 — depends on W1)
  │   │
  │   ├── fix/orders-list-pg-read      (W4.3/4.4/4.5 trio — independent)
  │   │
  │   ├── fix/performance-pg-read      (W4.1 — independent of W1)
  │   │
  │   ├── fix/portfolio-entry-date-pg  (W4.2 — depends on W1)
  │   │
  │   ├── feat/journal-pg              (W4.6/4.7 — depends on W1)
  │   │
  │   └── fix/pi-pg-read               (W4.8 — depends on W1)
  │
  ├── chore/remove-dual-writes         (W5 — after W4)
  │
  ├── feat/snapshot-freshness-observability (W6 — anytime, fold into W1 if early)
  │
  └── chore/<W7 per file>              (W7 — opportunistic)
```

**PR rule:** every PR must independently pass CI + paper-smoke + browser E2E. No "depends on next PR" branches; if a sequence is needed, ship them in order.

---

# Provenance

- Strategy doc: `docs/plans/2026-04-28-postgres-migration-completion.md`
- Predecessor plans (incorporated):
  - `docs/plans/2026-04-27-portfolio-postgres-read-path.md` (Phase 1 — shipped PR #56)
  - `docs/plans/2026-04-27-order-placement-reliability.md` (W1 prerequisites — shipped PR #61)
  - `docs/plans/2026-04-27-futu-postgres-migration-followup.md` (W4.9 — deferred)
- Memory references:
  - `project_postgres_migration_read_side_gap`
  - `feedback_in_process_route_bypass`
  - `feedback_testclient_skips_lifespan`
  - `feedback_ib_insync_permid_race`
  - `feedback_ib_insync_in_fastapi`
  - `feedback_broker_bugs_paper_first`

---

# Revision 1 (post-Codex review 2026-04-28)

> **Status:** Adopted. This section supersedes any conflicting tasks above. Companion strategy doc: `docs/plans/2026-04-28-postgres-migration-completion.md` § Revision 1.

## Symbol-name corrections (apply throughout)

| In Revision 0 | Correct symbol | Source |
|---|---|---|
| Stale terminal-state helper name | `mark_terminal` | `src/xenon/execution/orders_store.py:348` |
| Stale outbox schema name | `events.outbox` (schema `events`, not `xenon`) | `src/xenon/db/schema.py:720-732` |
| Stale execution pool role | `"data"` for read-only execs lookup; `"orders"` for placement; `"sync"` for boot rehydrate | `src/xenon/api/server.py:793,1733`; `src/xenon/api/ib_pool.py:98` |

## New tables / schema additions

```python
# src/xenon/db/schema.py — additions

order_fills = Table(
    "order_fills",
    xenon_metadata,
    Column("exec_id", Text, primary_key=True),  # IB-provided execId, immutable
    Column("submission_id", Text, ForeignKey(f"{XENON_SCHEMA}.order_submissions.submission_id"), nullable=True),
    Column("combo_attempt_id", Text, ForeignKey(f"{XENON_SCHEMA}.wizard_combo_attempts.attempt_id"), nullable=True),
    Column("perm_id", Text),
    Column("ib_order_id", Text),
    Column("con_id", BigInteger),
    Column("ticker", Text, nullable=False),
    Column("side", Text, nullable=False),  # BUY | SELL
    Column("qty", Integer, nullable=False),
    Column("price", Numeric(12, 4), nullable=False),
    Column("commission", Numeric(12, 4), server_default=text("0")),
    Column("filled_at", TIMESTAMP(timezone=True), nullable=False),
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False),  # NO legacy_unknown default
    Column("broker_account", Text, nullable=False),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_fills_broker"),
    CheckConstraint(
        "(submission_id IS NOT NULL) OR (combo_attempt_id IS NOT NULL) OR (metadata ? 'legacy_source')",
        name="ck_fills_source_present",
    ),
    Index("ix_fills_perm_id", "broker", "account_env", "broker_account", "perm_id"),
    Index("ix_fills_submission", "submission_id"),
    Index("ix_fills_combo_attempt", "combo_attempt_id"),
    Index("ix_fills_ticker_time", "ticker", "filled_at"),
)

# trades additions:
#   submission_id     Text, FK(order_submissions.submission_id), nullable=True
#   combo_attempt_id  Text, FK(wizard_combo_attempts.attempt_id), nullable=True
#   state             Text NOT NULL DEFAULT 'OPEN'
#                     CHECK (state IN ('OPEN','PARTIALLY_FILLED','CLOSED'))
```

---

## NEW Workstream W0 — Schema + Naming Foundation

**Branch:** `feat/order-fills-ledger-schema` · **Effort:** 1.5–2 days · **Blocks:** W1 entry

### Task W0.1: Add `xenon.order_fills` Table

**Files:**
- Modify: `src/xenon/db/schema.py`
- Create: Alembic migration `xxxx_add_order_fills.py`
- Create: `src/xenon/db/tests/test_order_fills_schema.py`

**Step 1: Failing tests**

```python
def test_order_fills_pk_is_exec_id(db_session): ...
def test_order_fills_check_requires_source(db_session):
    """Insert with submission_id=None AND combo_attempt_id=None AND no metadata.legacy_source → must fail."""
def test_order_fills_replay_is_idempotent(db_session):
    """Inserting the same exec_id twice → second raises IntegrityError."""
```

**Step 2: Migration + run**

```bash
uv run alembic revision -m "add order_fills execution-grain ledger"
# fill upgrade()/downgrade() with create_table per spec above
uv run alembic upgrade head
uv run pytest src/xenon/db/tests/test_order_fills_schema.py -q
```

**Step 3: Commit** — `feat(db): add xenon.order_fills execution-grain ledger`

### Task W0.2: Link `xenon.trades` To Fills + Add `state` Column

**Files:**
- Modify: `src/xenon/db/schema.py` (extend `trades`)
- Create: Alembic migration `xxxx_link_trades_to_fills.py`
- Modify: `src/xenon/db/tests/test_trades_schema.py`

**Migration body:**

```sql
ALTER TABLE xenon.trades
  ADD COLUMN submission_id TEXT REFERENCES xenon.order_submissions(submission_id),
  ADD COLUMN combo_attempt_id TEXT REFERENCES xenon.wizard_combo_attempts(attempt_id),
  ADD COLUMN state TEXT NOT NULL DEFAULT 'OPEN'
    CHECK (state IN ('OPEN','PARTIALLY_FILLED','CLOSED'));

UPDATE xenon.trades SET state = 'CLOSED' WHERE closed_at IS NOT NULL;
UPDATE xenon.trades SET state = 'OPEN'   WHERE closed_at IS NULL;

CREATE INDEX ix_trades_submission     ON xenon.trades(submission_id);
CREATE INDEX ix_trades_combo_attempt  ON xenon.trades(combo_attempt_id);
```

**Commit:** `feat(db): link trades to order_fills + add state column`

### Task W0.3: Lock Outbox Channel Contract

**Files:**
- Modify: `src/xenon/db/events.py`
- Create: `src/xenon/db/tests/test_outbox_channels.py`

**Step 1: Add channel constants and same-transaction emit helper**

```python
CHANNEL_FILL_RECORDED = "fill.recorded"
CHANNEL_TRADE_CLOSED  = "trade.closed"

def emit_outbox_in_txn(conn, *, channel: str, source: str, payload: dict) -> None:
    """Insert into events.outbox using the caller's existing connection.
    pg_notify fires post-commit → not racy when same connection writes the source row."""
    conn.execute(insert(outbox).values(channel=channel, source=source, payload=payload))
```

**Step 2: Validate channel naming against existing CHECK**

```python
def test_channel_constants_pass_outbox_check():
    assert len(CHANNEL_FILL_RECORDED) <= 63
    assert len(CHANNEL_TRADE_CLOSED) <= 63
    # dotted lowercase per src/xenon/db/events.py:16-29
```

**Commit:** `feat(events): lock outbox channel contract — fill.recorded + trade.closed`

### Task W0.4: Symbol-Name Sweep

```bash
rg -n "<stale terminal-helper|stale outbox-schema|stale execution-role spellings>" \
  --include="*.py" --include="*.md" --include="*.ts" --include="*.tsx"
```

Apply replacements per the table at top of Revision 1. Includes the IMPL plan above this section. Commit: `chore: correct symbol references in plan + tests`.

---

## REVISED Workstream W1 — Fill Capture (writes order_fills first)

**Replaces revision-0 W1.1 / W1.2 / W1.5.** W1.0 (diagnostic) and W1.3 (UNKNOWN fix) carry forward in intent. W1.4 backfill now targets `order_fills`, not `trades` directly.

### Task W1.1' (revised): `record_fill()` Helper

**Files:**
- Modify: `src/xenon/execution/orders_store.py` (add `record_fill`; keep `mark_terminal` separate)
- Create: `scripts/tests/test_record_fill.py`

**Spec:**

```python
def record_fill(
    *,
    exec_id: str,
    submission_id: str | None,
    combo_attempt_id: str | None = None,
    perm_id: str | None,
    ib_order_id: str | None = None,
    con_id: int | None,
    ticker: str,
    side: str,
    qty: int,
    price: Decimal,
    commission: Decimal = Decimal(0),
    filled_at: datetime,
    metadata: dict | None = None,
    broker: str,
    account_env: str,
    broker_account: str,
) -> bool:
    """Idempotent fill recording. Returns True if inserted, False if already present.
    Emits events.outbox(channel='fill.recorded') in the same transaction."""
```

Idempotency via `exec_id` PK uniqueness. Outbox emit uses `emit_outbox_in_txn(conn, ...)` with `source="record_fill"`.

**Failing tests:**

```python
def test_record_fill_inserts_row_and_emits_outbox_in_same_txn(db_session): ...
def test_record_fill_replay_is_idempotent(db_session): ...
def test_record_fill_rejects_missing_source_keys(db_session):
    """submission_id, combo_attempt_id, and legacy_source all None → CHECK fails."""
def test_record_fill_rejects_legacy_unknown_scope(db_session):
    """Codex minor: explicit scope required, no legacy_unknown silent default."""
```

**Commit:** `feat(orders): record_fill — idempotent execution-grain ledger writes`

### Task W1.2' (revised): Trade Aggregator

**Files:**
- Create: `src/xenon/execution/trade_aggregator.py`
- Create: `scripts/tests/test_trade_aggregator.py`

**Spec — pure derivation, idempotent:**

```python
def aggregate_trade_from_fills(
    *,
    submission_id: str | None = None,
    combo_attempt_id: str | None = None,
    legacy_id: str | None = None,
) -> None:
    """Read all order_fills matching the key. Compute entry_cost, exit_cost,
    realized_pnl, quantity, opened_at = min(filled_at), closed_at = max(filled_at)
    if balanced opening+closing fills exist else NULL. Upsert into xenon.trades
    keyed by (submission_id | combo_attempt_id | legacy_id).
    On OPEN→CLOSED transition, emit events.outbox(channel='trade.closed').
    Re-running with unchanged fills must produce identical trades row + no
    additional outbox row (track via state column transition, not emission)."""
```

**Failing tests:**

```python
def test_single_leg_two_partial_fills_yields_one_trades_row(): ...
def test_combo_legs_yield_one_trades_row_with_metadata_legs(): ...
def test_close_emits_trade_closed_outbox_once(): ...
def test_aggregate_is_idempotent(): ...
def test_aggregate_handles_legacy_only_fills_without_submission(): ...
```

**Commit:** `feat(orders): trade_aggregator derives xenon.trades from order_fills`

### Task W1.3' (revised): Wire Live Fill Events Through `record_fill` + Aggregator

**Files:**
- Modify: `src/xenon/execution/single_leg_rehydrate.py` (FILLED + PARTIALLY_FILLED branches)
- Modify: `src/xenon/execution/ib_execute.py:317-359` (legacy CLI path)
- Modify: `src/xenon/execution/combo_wizard/rehydrate.py:144-170`

**Pattern at every fill-receipt site:**

```python
# 1. For each IB execution returned by reqExecutions:
for exec_record in executions_for_perm_id:
    record_fill(
        exec_id=exec_record.execId,
        submission_id=submission_id,        # or combo_attempt_id, or None+legacy_source
        perm_id=str(perm_id),
        ib_order_id=str(ib_order_id) if ib_order_id else None,
        con_id=con_id,
        ticker=ticker,
        side=exec_record.side,              # BOT → BUY, SLD → SELL
        qty=exec_record.shares,
        price=Decimal(str(exec_record.price)),
        commission=Decimal(str(commission_for_exec(exec_record))),
        filled_at=exec_record.time,
        broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account,
    )

# 2. Re-derive the trades row:
aggregate_trade_from_fills(submission_id=submission_id)  # or combo_attempt_id

# 3. Update the state machine:
mark_terminal(
    submission_id=submission_id,
    state="FILLED" if fully_filled else "PARTIALLY_FILLED",
    reason_code=None,
    filled_qty=total_filled_qty,
    avg_fill_price=weighted_avg_price,
)
```

**Legacy CLI (`ib_execute.py`):** writes fills with `submission_id=None` and `metadata={"legacy_source": "ib_execute_cli", "legacy_id": str(exec_record.execId)}`. Aggregator picks these up via `legacy_id` lookup. The CHECK constraint accepts because `metadata ? 'legacy_source'`.

**Combo wizard (`combo_wizard/rehydrate.py`):** iterates per-leg execs, calls `record_fill(combo_attempt_id=...)` for each. Aggregator detects shared `combo_attempt_id` and produces one combo trades row with `metadata.legs[]`.

**Paper-smoke matrix:**

For each scenario, assert PG state in this order: rows in `order_fills` → row in `trades` (aggregated) → rows in `events.outbox` for both `fill.recorded` and (if closed) `trade.closed`.

1. Stock BUY → fill → 1 fill, 1 open trade row.
2. Stock SELL closing → 1 additional fill, trade row updates: `state=CLOSED`, `closed_at` set, `realized_pnl` populated.
3. Single-leg option BUY → fill → 1 fill + 1 trade.
4. Combo (vertical spread) BUY → fill → N leg fills + 1 combo trade with `metadata.legs[]`.
5. PARTIALLY_FILLED on partial fill → 1 fill, trade row `state=PARTIALLY_FILLED`.
6. Replay: rerun rehydrate with no new fills → 0 inserts, 0 outbox emits.

**Commit:** `feat(rehydrate): route fills through order_fills + aggregator; close UNKNOWN gap`

### Task W1.4' (revised): Backfill `order_fills` From `trade_log.json`

**Files:**
- Create: `scripts/migrations/_2026_04_28_backfill_fills_from_trade_log.py`

For every legacy `trade_log.json` entry, derive a synthetic `exec_id = sha256(ticker|opened_at|qty|price)` and write to `order_fills` with `submission_id=None`, `metadata={"legacy_source": "trade_log_json", "legacy_id": <stable_hash>}`. Then call `aggregate_trade_from_fills(legacy_id=...)` per group.

Idempotent via PK + stable hash. Re-runs produce no new rows.

**Commit:** `feat(migration): backfill xenon.order_fills from legacy trade_log.json`

---

## REVISED W4.1 — Performance (now two sub-tasks)

### Task W4.1.A (NEW): Migrate `portfolio_performance.py` To PG

**Files:**
- Modify: `src/xenon/reports/portfolio_performance.py:143-149,215-220,1207-1210,1324-1332`

Replace `data/portfolio.json` reads with `account_snapshots.payload` query. Replace `data/blotter.json` reads with `xenon.trades` query (post-W1).

**CI guard test:**

```python
def test_portfolio_performance_does_not_read_json_files(monkeypatch):
    """Stub builtins.open to raise if path contains data/portfolio.json or data/blotter.json."""
```

**Commit:** `feat(performance): postgres-backed portfolio_performance.py`

### Task W4.1.B (was W4.1): Route Migration + TTL Cache

Same as Revision 0 W4.1 but now safe because the script no longer reads JSON. Add route-layer TTL cache (Q3=A): 5min open / 30min closed via `XENON_PERFORMANCE_TTL_OPEN_S` / `XENON_PERFORMANCE_TTL_CLOSED_S`. CI guard tests cover **both** `performance.json` AND `portfolio.json` (Codex finding #8).

---

## PROMOTED Workstream W7.8 — `ib_reconcile` Becomes `order_fills` Writer (now Week 2A)

**Branch:** `feat/ib-reconcile-pg-fills-writer` · **Effort:** 1 day · **Blocks:** W4.7

### Task W7.8.1 (promoted)

**Files:**
- Modify: `src/xenon/execution/ib_reconcile.py:5-15,279-281`

Replace `portfolio.json` read with `account_snapshots.payload` query. Replace `trade_log.json` append with `record_fill()` calls — idempotent via `exec_id` PK so external IB fills already in the ledger are skipped automatically. After the batch, call `aggregate_trade_from_fills` for every distinct affected submission/legacy group.

**Test:** given 5 IB executions with 3 already in `order_fills`, assert exactly 2 new rows + aggregator runs for the affected groups.

**Commit:** `feat(reconcile): ib_reconcile writes external fills to xenon.order_fills`

---

## REVISED Acceptance Criteria (additions over Revision 0)

- [ ] W0 ships before any W1 task touches code.
- [ ] Every IB fill (Xenon-placed and external) lands in `xenon.order_fills` exactly once (idempotent).
- [ ] `xenon.trades` is reproducible by re-running `aggregate_trade_from_fills` over `order_fills`.
- [ ] `events.outbox` emits channels `fill.recorded` and `trade.closed` in the same transaction as their source writes.
- [ ] `portfolio_performance.py` reads PG only — confirmed by `test_portfolio_performance_does_not_read_json_files`.
- [ ] Zero residual references to the stale terminal-helper name, stale outbox-schema name, or stale execution pool role anywhere in code/plans/tests/docs.
- [ ] `BlotterData` type in `web/lib/types.ts:402-411` includes `configured?: boolean` and `source?: "postgres" | "flex" | "postgres+flex" | "none"`.
- [ ] `/api/blotter` route guard test exists alongside other CI guards (Codex finding #8).
- [ ] No new W1/W7.8 write silently accepts `legacy_unknown` scope defaults — explicit scope required at every call site.

---

## REVISED Branching + PR Strategy

```
master ── PR #61 (shipped)
  │
  ├── fix/blotter-empty-state                  (W2 — independent)
  │
  ├── feat/order-fills-ledger-schema           (W0 — NEW; blocks W1)
  │
  └── feat/fill-capture-pipeline               (W1 — depends on W0)
      │
      ├── feat/ib-reconcile-pg-fills-writer    (W7.8 — depends on W0 + W1.1')
      ├── feat/blotter-pg-first                (W3 — depends on W1.1' + W1.2')
      ├── fix/orders-list-pg-read              (W4.3-4.5 trio — independent)
      │
      ├── feat/portfolio-perf-pg               (W4.1.A — independent of W1)
      ├── fix/performance-pg-read              (W4.1.B — depends on W4.1.A)
      │
      ├── fix/portfolio-entry-date-pg          (W4.2 — depends on W1)
      │
      ├── feat/journal-pg                      (W4.6 — depends on W1 + W7.8)
      ├── refactor/journal-sync-pg-listener    (W4.7 — depends on W4.6 + W7.8)
      │
      └── fix/pi-pg-read                       (W4.8 — depends on W1)

  ├── chore/remove-dual-writes                 (W5 — after W4)
  ├── feat/snapshot-freshness-observability    (W6 — anytime)
  └── chore/<W7 per file>                      (W7 — opportunistic; W7.8 promoted above)
```

## Errata

### 2026-04-28 — W0.1 `ck_fills_source_present` Must Guard NULL Metadata

- Evidence: `docs/plans/2026-04-28-postgres-migration-completion-IMPL.md:1298-1300` specified `(metadata ? 'legacy_source')` as the legacy-source branch of the source-present check.
- Local Postgres probe: `SELECT (NULL::jsonb ? 'legacy_source'), ((NULL::text IS NOT NULL) OR (NULL::text IS NOT NULL) OR (NULL::jsonb ? 'legacy_source'));` returns NULL for both expressions. PostgreSQL CHECK constraints pass when the expression evaluates to NULL, so the planned check would allow rows with `submission_id IS NULL`, `combo_attempt_id IS NULL`, and `metadata IS NULL`.
- Correction for W0.1: use `submission_id IS NOT NULL OR combo_attempt_id IS NOT NULL OR (metadata IS NOT NULL AND metadata ? 'legacy_source')`.
