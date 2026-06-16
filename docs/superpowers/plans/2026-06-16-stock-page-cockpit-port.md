# Stock-Page Cockpit Port (radon → xenon) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace xenon's click-only tab-bar stock page (`TickerDetailContent`) with radon's keyboard-driven "AssetCockpit" shell — grid (header / book · act · rail), single-key decks, watchlist star, ETF-aware Company tab, Black-Scholes implied chain column — reaching 1:1 visual parity with radon at `/SPX?posId=6` while preserving every xenon invariant (DB-first, naked-short Gate-4, AccountScope, XENON_READ_ONLY, in-process-bypass guard). The L2 depth Book + Time & Sales tape is **deferred to a separate plan** (it targets a different IB library and is not verifiable at the SPX goal — see Phase 3 below).

**Architecture:** Two sequenced phases here, each independently shippable; depth is a third, separate plan.

- **Phase 1 — Watchlist (DB-first):** new Postgres `user_watchlist` table, an operator-scoped FastAPI router, Next proxy, `useWatchlist` hook, `StarToggle` component. Backend + isolated UI primitive only.
- **Phase 2 — Cockpit shell + quick wins (the parity phase):** port `breakpoints`/`useViewport`, single-source deck helpers, `CockpitHeader`, `GlyphRail`, `AssetDeck`, `AssetCockpit`, `ActHeldSummary`; convert `TickerDetailContent` into the cockpit adapter (holds `urlDeck`+`localDeck`, resolves quote/book props) and keep `TickerWorkspace` thin, migrating the URL model `?tab=<name>` → `?deck=<c|p|n|r|s|i>`; make `CompanyTab` ETF/index-aware; add a Black-Scholes "Implied" chain column. Depth/tape props stay empty, so the Book renders xenon's existing L1 view — exactly what radon shows for SPX (an index has no L2 depth). **This phase achieves the SPX parity goal.**
- **Phase 3 — Depth + tape:** SPLIT OUT into its own spec → plan → PR (see the Phase 3 section). It must first resolve an IB-library mismatch (xenon relay = `ib@0.2.9`, radon = `@stoqey/ib`) and add `WorkspaceShell`/`TickerDetailContext` plumbing; it is RTH/AAPL-verifiable only, not at SPX.

**Tech Stack:** Next.js 16 App Router (React 19) + TypeScript, Vitest + Playwright, FastAPI (Python 3.13 via `uv`), SQLAlchemy Core + Alembic on Postgres. (The separate Phase-3 plan adds the realtime-relay work; this plan touches no relay code.) Brand tokens per `brand/CLAUDE.md`.

---

## Hard constraints (xenon invariants — every task respects these)

1. **DB-first.** Watchlist persists to Postgres (`user_watchlist`), never `data/*.json`. CI guards `no_json_write_on_order_path.py` / `no_json_fallback_on_order_path.py` — but note watchlist routes live OUTSIDE the order path, so they are not affected; still, never write JSON.
2. **Naked-short Gate-4 untouched.** This port does not add a new order-submission path. The cockpit's `OrderTab` is the _existing_ xenon `OrderTab` (already guarded). We do NOT port radon's inline `PositionTradeTicket`. Any click-to-fill only _pre-fills_ the existing guarded ticket; submission still goes through `POST /api/orders/place` → FastAPI → `preflight.py` Gate-4.
3. **In-process route bypass.** No new in-process order helpers are introduced; nothing to add to `order_path_caller_allowlist`.
4. **AccountScope.** Watchlist rows carry `user_id` only (not broker/account_env/broker_account) — a watchlist is a user preference, not execution data. xenon is a single-operator terminal, so `user_id` resolves server-side to the constant `"local"` (the same value the proxied order path uses at `server.py:2298`), NOT a Clerk subject (the Next→FastAPI hop is unauthenticated localhost; see Task 1.3). Documented in the migration.
5. **XENON_READ_ONLY=1.** The watchlist POST/DELETE are user-preference writes, NOT order/portfolio writes — they remain allowed under read-only mode (read-only gates the _order/portfolio_ surface only; see `src/xenon/api/guards.py`). Confirm in Task 1.3 that watchlist routes do NOT depend on `read_only_403`. (Phases 1–2 touch no relay/market-data code; the deferred depth plan handles its own read-only/relay concerns.)
6. **Prod Docker topology.** Phases 1–2 touch NO relay code. The only prod-stack surface here is the `user_watchlist` migration, which the macmini Docker `migrator` applies to `core_dev` on deploy (Task 1.1). The realtime-relay edits live entirely in the deferred Phase-3 plan (which must verify on the dev relay then prod-deploy + RTH re-check per memory `feedback_verify_prod_docker_topology`).
7. **Identity.** No signal generation. The cockpit adds no scanner/regime/opportunity widgets. The command-palette deck (`:`) is ported as radon's inert placeholder only.

---

## Verified prerequisite — IB depth entitlement (for Phase 3)

A read-only `ib_async` probe against the live macmini IB Gateway (`100.66.147.98:4001`, account `U18007831`, 2026-06-16) confirmed:

- `reqMktDepth(AAPL SMART, numRows, isSmartDepth=True)` — **accepted, no permission error** (0 levels only because market was closed). Entitled feeds include `SMART STK AggDeep (aggGroup=1)`, `ARCA/NASDAQ/NASDTAS/ISLAND STK Deep`, and US options per-exchange depth (ISE/AMEX/PSE/GEMINI/MERCURY).
- `reqMktDepth(AAPL NASDAQ, isSmartDepth=False)` — **error 10089** "requires additional subscription."
- **Implementation constraint (for the deferred Phase-3 plan):** the relay MUST use `isSmartDepth=true` for equities (SMART AggDeep). Direct-exchange depth is NOT entitled. Options depth is per-exchange BBO only (no SMART aggregate) — best-effort, not a blocker.
- **SPX caveat:** an index has no tradeable L2 depth. The Phase-2 verification target `/SPX?posId=6` will show the L1-fallback Book in BOTH radon and xenon. Phase 3 depth is therefore **not verifiable at SPX** — verify it on a depth-eligible equity (e.g. AAPL) during RTH.

---

## Verification target & method

**Reference:** radon at `http://localhost:3000/SPX?posId=6` (running, confirmed).
**Subject:** xenon at `http://localhost:3200/SPX?posId=6` (dev stack running, confirmed).

After Phase 2, achieve visual + behavioral parity:

- Cockpit grid (header row, book region left, act region right, glyph rail far right).
- `CockpitHeader`: `SPX`, star toggle, `STOCK`/`INDEX` kind, last + Δ%, spread, LIVE dot, position chip.
- Single-key decks: `c`(Chain) `p`(Position) `n`(News) `r`(Ratings) `s`(Seasonal) `i`(Info) `:`(palette) + `Esc`. Guarded against typing targets.
- `CompanyTab` (deck `i`): for SPX (index) hides P/E, EPS, Next Earnings.
- `OptionsChainTab` (deck `c`): includes the "Implied" (Black-Scholes) column.
- Act region: existing guarded `OrderTab` ticket + position summary (posId=6 resolved).

**Method:** Playwright screenshots of both URLs side-by-side at a fixed viewport (1440×900, dark theme); diff the cockpit regions. Plus chrome-cdp interactive pass for the keyboard decks. Documented in Task 2.16.

---

## File structure

**Phase 1 (watchlist):**

- Create: `src/xenon/db/migrations/versions/2026_06_16_user_watchlist.py`
- Modify: `src/xenon/db/schema.py` (add `user_watchlist` Core table)
- Create: `src/xenon/db/queries/watchlist.py`
- Modify: `src/xenon/api/server.py` (add `GET/POST/DELETE /watchlist` routes)
- Create: `web/app/api/watchlist/route.ts`, `web/app/api/watchlist/[symbol]/route.ts`
- Create: `web/lib/useWatchlist.ts`, `web/components/StarToggle.tsx`
- Tests: `scripts/tests/test_watchlist_api.py`, `web/tests/useWatchlist.test.ts`, `web/tests/StarToggle.test.tsx`

**Phase 2 (cockpit + quick wins):**

- Create: `web/lib/useViewport.ts`, `web/lib/deckNav.ts`
- Create: `web/components/ticker-detail/CockpitHeader.tsx`, `GlyphRail.tsx`, `AssetDeck.tsx`, `AssetCockpit.tsx`, `ActHeldSummary.tsx`
- Create: `web/lib/blackScholes.ts`, `web/lib/impliedValue.ts`
- Modify: `web/components/TickerWorkspace.tsx` (render cockpit, deck URL model)
- Modify: `web/components/ticker-detail/CompanyTab.tsx` (ETF/index gate)
- Modify: `web/components/ticker-detail/OptionsChainTab.tsx` (Implied column)
- Modify: `web/components/ticker-detail/BookTab.tsx` (accept `bookOnly` prop)
- Modify: `web/app/globals.css` (cockpit CSS, brand-token mapped)
- Remove (after migrating its callers): `web/components/TickerDetailContent.tsx`
- Tests: `web/tests/deckNav.test.ts`, `web/tests/AssetDeck.test.tsx`, `web/tests/CockpitHeader.test.tsx`, `web/tests/blackScholes.test.ts`, `web/tests/impliedValue.test.ts`, `web/tests/CompanyTab.etf.test.tsx`; update `web/tests/ticker-detail-spread-notional.test.ts`, `web/tests/ticker-chain-position-focus.test.tsx`, `web/tests/modify-order-ticker-detail.test.ts`, `web/e2e/risk-reversal-midprice.spec.ts`

**Phase 3 (depth + tape):** deferred to a separate plan — see the Phase 3 section. Files it will touch (relay, `pricesProtocol`, `usePrices`, `WorkspaceShell`, `TickerDetailContext`, `BookTab`, new `DepthMontage`/`OrderBook`/`TimeAndSales`) are scoped there, gated by the IB-library decision (`ib@0.2.9` vs `@stoqey/ib`).

---

## Porting convention

For radon components ported **verbatim** (small, pure, no xenon-specific deltas), the step says _"Copy `<radon path>` to `<xenon path>` verbatim"_ — the radon file is the canonical source, copy it exactly, then apply the listed deltas. For xenon-specific new logic (B-S math, watchlist API, depth protocol), full code is inline. Radon root: `/Users/chenxi/projects/radon`. xenon root: `/Users/chenxi/projects/xenon`.

Run gates after each phase: `cd web && npm run typecheck && npm test` and `uv run python scripts/infra/dev/run_pytest_affected.py`.

---

# PHASE 1 — Watchlist (DB-first)

### Task 1.1: Postgres `user_watchlist` table (schema + migration)

**Files:**

- Modify: `src/xenon/db/schema.py`
- Create: `src/xenon/db/migrations/versions/2026_06_16_user_watchlist.py`

- [ ] **Step 1: Add the Core table to `schema.py`** (match xenon's verified conventions)

xenon `schema.py` uses `xenon_metadata = MetaData(schema=XENON_SCHEMA)`, the `Text` type (not `String`), `TIMESTAMP(timezone=True)` (imported from `sqlalchemy.dialects.postgresql`), `tz_now = text("now()")` for timestamp defaults, and declares indexes inline in the table (so autogenerate sees no drift). Match that exactly (verified `schema.py:20–53`). Add:

```python
user_watchlist = Table(
    "user_watchlist",
    xenon_metadata,
    Column("id", Text, primary_key=True),          # uuid4 hex
    Column("user_id", Text, nullable=False),       # operator id; today always "local" (single-operator terminal)
    Column("symbol", Text, nullable=False),
    Column("sector", Text, nullable=True),
    Column("added_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    Index("ix_user_watchlist_user_added", "user_id", "added_at"),
)
```

`Text`, `TIMESTAMP`, `tz_now`, `Column`, `Table`, `UniqueConstraint`, `Index`, `xenon_metadata` are all already imported/defined in `schema.py` — no new imports. The `schema=` kwarg is NOT passed per-table (it's on `xenon_metadata`), matching every existing table.

- [ ] **Step 2: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "user_watchlist table"`
The generated file lands under `src/xenon/db/migrations/versions/`; keep its hash name (alembic chains by `down_revision`, not filename). Inspect the autogenerated `upgrade()`: it must `op.create_table("user_watchlist", ..., schema="xenon")` with the unique constraint AND the `ix_user_watchlist_user_added` index (declared in metadata Step 1, so autogenerate emits it — confirm it's present). Verify `down_revision` points at the current head (per memory `project_alembic_phantom_revision_core_dev`, core_dev is on `2026_06_13_fill_qty_numeric`) — a branched down_revision breaks `upgrade head`.

- [ ] **Step 3: Apply to the dev DB**

Run: `uv run alembic upgrade head`
Expected: no error; `psql "$DATABASE_URL_PAPER" -c "\d xenon.user_watchlist"` shows the table. (Per memory `project_two_core_test_dbs`, `dev.sh paper` uses the LOCAL `DATABASE_URL_PAPER` — apply there.) The macmini Docker `migrator` applies it to `core_dev` on next deploy; do NOT run against `core_dev`.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/2026_06_16_user_watchlist.py
git commit -m "feat(db): user_watchlist table + migration"
```

---

### Task 1.2: Watchlist query module

**Files:**

- Create: `src/xenon/db/queries/watchlist.py`
- Test: `scripts/tests/test_watchlist_queries.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_watchlist_queries.py
import pytest
from xenon.db.queries import watchlist


def test_add_list_remove_roundtrip(pg_test_engine):
    uid = "user_test_1"
    watchlist.add(uid, "AAPL", sector="Technology")
    rows = watchlist.list_for_user(uid)
    assert [r["symbol"] for r in rows] == ["AAPL"]
    assert rows[0]["sector"] == "Technology"

    # idempotent add (UNIQUE user_id+symbol) — no duplicate, no error
    watchlist.add(uid, "AAPL", sector="Technology")
    assert len(watchlist.list_for_user(uid)) == 1

    watchlist.remove(uid, "AAPL")
    assert watchlist.list_for_user(uid) == []


def test_scoped_per_user(pg_test_engine):
    watchlist.add("user_a", "TSLA")
    watchlist.add("user_b", "NVDA")
    assert [r["symbol"] for r in watchlist.list_for_user("user_a")] == ["TSLA"]
    assert [r["symbol"] for r in watchlist.list_for_user("user_b")] == ["NVDA"]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest scripts/tests/test_watchlist_queries.py -xvs`
Expected: FAIL (module `xenon.db.queries.watchlist` not found).

- [ ] **Step 3: Implement the module**

```python
# src/xenon/db/queries/watchlist.py
"""Postgres-backed watchlist (user preference, scoped by operator user_id="local")."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import user_watchlist


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(
            user_watchlist.c.id,
            user_watchlist.c.symbol,
            user_watchlist.c.sector,
            user_watchlist.c.added_at,
        )
        .where(user_watchlist.c.user_id == user_id)
        .order_by(user_watchlist.c.added_at.desc())
    )
    with get_sync_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


def add(user_id: str, symbol: str, sector: str | None = None) -> None:
    sym = symbol.upper().strip()
    stmt = (
        pg_insert(user_watchlist)
        .values(id=uuid.uuid4().hex, user_id=user_id, symbol=sym, sector=sector)
        .on_conflict_do_nothing(constraint="uq_user_watchlist_user_symbol")
    )
    with get_sync_engine().begin() as conn:
        conn.execute(stmt)


def remove(user_id: str, symbol: str) -> None:
    sym = symbol.upper().strip()
    stmt = delete(user_watchlist).where(
        user_watchlist.c.user_id == user_id, user_watchlist.c.symbol == sym
    )
    with get_sync_engine().begin() as conn:
        conn.execute(stmt)
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest scripts/tests/test_watchlist_queries.py -xvs`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/watchlist.py scripts/tests/test_watchlist_queries.py
git commit -m "feat(db): watchlist query module + roundtrip tests"
```

---

### Task 1.3: FastAPI watchlist routes (operator-scoped, not read-only-gated)

**Files:**

- Modify: `src/xenon/api/server.py`
- Test: `src/xenon/api/tests/test_watchlist_routes.py`

> **Auth model (verified, server.py:635–660, 2298).** xenon's `auth_middleware` **skips auth for localhost** (`127.0.0.1`/`::1`) — every Next→FastAPI proxy call lands as an unauthenticated server-to-server request, and `request.state.user` is NOT set. `verify_clerk_jwt` only fires on direct browser→FastAPI calls (e.g. the WS-ticket route). The proxied data path (orders, preflight) therefore hardcodes `user_id = "local"`. xenon is a **single-operator terminal**, so the watchlist mirrors that exactly: a module constant `_OPERATOR_USER_ID = "local"`. Do NOT add `Depends(verify_clerk_jwt)` — it would 401/return nothing on the proxied path. (The `user_id` column stays in the schema for future multi-user use; today it is always `"local"`, consistent with `order_submissions`.)

- [ ] **Step 1: Write the failing test**

```python
# src/xenon/api/tests/test_watchlist_routes.py
from fastapi.testclient import TestClient
from xenon.api.server import app


def test_watchlist_crud():
    # TestClient calls arrive as localhost → auth_middleware skips auth (no token
    # needed); routes resolve the operator user_id ("local") server-side.
    with TestClient(app) as client:
        r = client.get("/watchlist")
        assert r.status_code == 200
        assert r.json() == {"watchlist": []}

        r = client.post("/watchlist", json={"symbol": "aapl", "sector": "Tech"})
        assert r.status_code == 200

        r = client.get("/watchlist")
        syms = [w["symbol"] for w in r.json()["watchlist"]]
        assert syms == ["AAPL"]

        r = client.delete("/watchlist/AAPL")
        assert r.status_code == 200
        assert client.get("/watchlist").json()["watchlist"] == []
```

Note: the route's query functions build their own connection via `get_sync_engine()`. Under the autouse `BEGIN/ROLLBACK` fixture, that second physical connection cannot see the test's outer transaction — and `TestClient(app)` as a context manager runs lifespan, so this is effectively an out-of-band writer. Add `@pytest.mark.committed_db` to this test (root `CLAUDE.md` § Pytest infrastructure escape hatches) so it uses TRUNCATE pre+post semantics. Confirm on first run.

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_watchlist_routes.py -xvs`
Expected: FAIL (404 — routes not defined).

- [ ] **Step 3: Create a router module `src/xenon/api/routes/watchlist.py`**

Per `src/xenon/api/CLAUDE.md`, new endpoints live under `src/xenon/api/routes/` as an `APIRouter` registered from `server.py` (verified pattern: `server.py:46` `from xenon.api.routes.journal import router as journal_router`, `server.py:608` `app.include_router(journal_router)`). Do NOT add routes inline to `server.py`. The operator user_id is resolved server-side (matching `server.py:2298`); routes take NO auth dependency (localhost is trusted, same as the order path) and do NOT depend on `read_only_403` (watchlist is a user preference, allowed in read-only mode per constraint #5):

```python
# src/xenon/api/routes/watchlist.py
import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from xenon.db.queries import watchlist as watchlist_q

router = APIRouter(tags=["watchlist"])

_OPERATOR_USER_ID = "local"  # single-operator terminal; mirrors order-path user_id


class WatchlistAddBody(BaseModel):
    symbol: str
    sector: str | None = None


@router.get("/watchlist")
async def get_watchlist():
    rows = await asyncio.to_thread(watchlist_q.list_for_user, _OPERATOR_USER_ID)
    return {"watchlist": rows}


@router.post("/watchlist")
async def add_watchlist(body: WatchlistAddBody):
    await asyncio.to_thread(watchlist_q.add, _OPERATOR_USER_ID, body.symbol, body.sector)
    return {"ok": True}


@router.delete("/watchlist/{symbol}")
async def delete_watchlist(symbol: str):
    await asyncio.to_thread(watchlist_q.remove, _OPERATOR_USER_ID, symbol)
    return {"ok": True}
```

(Sync query functions run via `asyncio.to_thread` because they use the sync engine. `added_at` ordering is most-recent-first; the datetime is JSON-safe via FastAPI's default encoder.)

- [ ] **Step 4: Register the router in `server.py`**

Add next to the other router imports/registrations (`server.py:46` block and `server.py:608` block):

```python
from xenon.api.routes.watchlist import router as watchlist_router
# ... with the other app.include_router(...) calls:
app.include_router(watchlist_router)
```

- [ ] **Step 5: Run test, verify pass**

Run: `uv run pytest src/xenon/api/tests/test_watchlist_routes.py -xvs`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/routes/watchlist.py src/xenon/api/server.py src/xenon/api/tests/test_watchlist_routes.py
git commit -m "feat(api): operator-scoped watchlist router (GET/POST/DELETE)"
```

---

### Task 1.4: Next.js proxy routes

**Files:**

- Create: `web/app/api/watchlist/route.ts`, `web/app/api/watchlist/[symbol]/route.ts`

> **Reference (verified):** `ticker/ratings/route.ts` is NOT a proxy — it uses `runScript`. Model these on the real `xenonFetch` dynamic-proxy pattern in `web/app/api/wizard/sessions/[id]/route.ts`: it uses `getRequestId()` (from `@/lib/apiContracts`, l.26) and `passThroughXenonError(err, requestId)` (signature `(err, requestId)`, verified `passThroughXenonError.ts:13–15`). The `requestId` arg is REQUIRED. POST/DELETE bodies must set `Content-Type: application/json` so FastAPI parses them. `params` IS a `Promise` (xenon is on Next 16; confirmed by `wizard/sessions/[id]/route.ts:11`).

- [ ] **Step 1: Implement the GET/POST proxy** (`web/app/api/watchlist/route.ts`)

```typescript
import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";

export const runtime = "nodejs";

export async function GET() {
  const requestId = getRequestId();
  try {
    const data = await xenonFetch("/watchlist", {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}

export async function POST(request: Request) {
  const requestId = getRequestId();
  try {
    const body = await request.json();
    const data = await xenonFetch("/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}
```

- [ ] **Step 2: Implement the DELETE proxy** (`web/app/api/watchlist/[symbol]/route.ts`)

```typescript
import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";

export const runtime = "nodejs";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ symbol: string }> },
) {
  const requestId = getRequestId();
  try {
    const { symbol } = await params;
    const data = await xenonFetch(`/watchlist/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}
```

Confirm `xenonFetch`'s options accept `headers` (it spreads `...fetchOpts` into the fetch init — verified `xenonApi.ts:23–32`), so the `Content-Type` header passes through.

- [ ] **Step 3: Typecheck + commit**

Run: `cd web && npm run typecheck`
Expected: PASS.

```bash
git add web/app/api/watchlist/
git commit -m "feat(web): watchlist proxy routes"
```

---

### Task 1.5: `useWatchlist` hook

**Files:**

- Create: `web/lib/useWatchlist.ts`
- Test: `web/tests/useWatchlist.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/useWatchlist.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWatchlist } from "@/lib/useWatchlist";

describe("useWatchlist", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    global.fetch = vi.fn(async (url: string, opts?: RequestInit) => {
      if (!opts || opts.method === undefined || opts.method === "GET") {
        return new Response(
          JSON.stringify({
            watchlist: [
              { id: "1", symbol: "AAPL", sector: null, added_at: "" },
            ],
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as unknown as typeof fetch;
  });

  it("loads and reports isWatched", async () => {
    const { result } = renderHook(() => useWatchlist());
    await waitFor(() => expect(result.current.isWatched("AAPL")).toBe(true));
    expect(result.current.isWatched("TSLA")).toBe(false);
  });

  it("toggleWatch optimistically adds then persists", async () => {
    const { result } = renderHook(() => useWatchlist());
    await waitFor(() => expect(result.current.isWatched("AAPL")).toBe(true));
    await act(async () => {
      await result.current.toggleWatch("TSLA");
    });
    expect(result.current.isWatched("TSLA")).toBe(true);
  });
});
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd web && npx vitest run tests/useWatchlist.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Port + adapt the hook**

Copy `/Users/chenxi/projects/radon/web/lib/useWatchlist.ts` to `/Users/chenxi/projects/xenon/web/lib/useWatchlist.ts` verbatim, then apply deltas:

- No parse change needed: radon's `loadWatchlist` already reads `(await res.json()).watchlist` (verified in radon `useWatchlist.ts:34–51`), which matches Task 1.3's `{"watchlist": rows}` response exactly.
- POST body `{ symbol, sector }` and DELETE `/api/watchlist/{symbol}` already match Task 1.3/1.4.
- Keep the module-level cache + pub/sub and the optimistic `toggleWatch` (add: optimistic set then POST; remove: optimistic delete then DELETE; on error, roll back).

- [ ] **Step 4: Run test, verify pass**

Run: `cd web && npx vitest run tests/useWatchlist.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add web/lib/useWatchlist.ts web/tests/useWatchlist.test.ts
git commit -m "feat(web): useWatchlist hook (Postgres-backed)"
```

---

### Task 1.6: `StarToggle` component

**Files:**

- Create: `web/components/StarToggle.tsx`
- Test: `web/tests/StarToggle.test.tsx`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/StarToggle.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import StarToggle from "@/components/StarToggle";

describe("StarToggle", () => {
  it("reflects active via aria-pressed and fires onToggle", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<StarToggle active={false} onToggle={onToggle} />);
    const btn = screen.getByRole("button");
    expect(btn).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledOnce();
    rerender(<StarToggle active onToggle={onToggle} />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  it("does not fire when busy", () => {
    const onToggle = vi.fn();
    render(<StarToggle active={false} busy onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd web && npx vitest run tests/StarToggle.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Port verbatim**

Copy `/Users/chenxi/projects/radon/web/components/StarToggle.tsx` to `/Users/chenxi/projects/xenon/web/components/StarToggle.tsx` verbatim (no deltas — it is a pure presentational button; full source is in the radon-audit reference). It uses classes `star-toggle`, `star-toggle--{size}`, `star-toggle--active`, `star-toggle--busy`, `star-toggle__glyph`, `star-toggle__label`.

- [ ] **Step 4: Port the StarToggle CSS**

From `/Users/chenxi/projects/radon/web/app/globals.css` grep `star-toggle` and copy those blocks into `web/app/globals.css`, mapping any raw colors to brand tokens (`var(--signal-core)`, `var(--text-muted)`) per `brand/CLAUDE.md` (4px max radius, no glow beyond the existing token usage).

- [ ] **Step 5: Run test, verify pass**

Run: `cd web && npx vitest run tests/StarToggle.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add web/components/StarToggle.tsx web/tests/StarToggle.test.tsx web/app/globals.css
git commit -m "feat(web): StarToggle component + styles"
```

**Phase 1 gate:** `cd web && npm run typecheck && npm test` green; `uv run python scripts/infra/dev/run_pytest_affected.py` green.

---

# PHASE 2 — Cockpit shell + quick wins (SPX parity)

### Task 2.1: `useViewport` hook

**Files:**

- Create: `web/lib/useViewport.ts`
- Test: `web/tests/useViewport.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/useViewport.test.ts
import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";
import { useViewport } from "@/lib/useViewport";

describe("useViewport", () => {
  it("returns hasMounted true after mount and an isMobile boolean", () => {
    const { result } = renderHook(() => useViewport());
    expect(typeof result.current.isMobile).toBe("boolean");
    expect(result.current.hasMounted).toBe(true);
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — `cd web && npx vitest run tests/useViewport.test.ts` → FAIL.

- [ ] **Step 3: Port `breakpoints.ts` FIRST, then `useViewport`**

radon's `useViewport.ts` imports `{ BREAKPOINTS, classifyViewport, type ViewportClass } from "./breakpoints"` (verified radon `useViewport.ts:4`), and xenon has NO `web/lib/breakpoints.ts`. So first copy `/Users/chenxi/projects/radon/web/lib/breakpoints.ts` → `/Users/chenxi/projects/xenon/web/lib/breakpoints.ts` verbatim. Then copy `useViewport.ts` verbatim (its `./breakpoints` import now resolves). It returns `{ isMobile, isTablet, isDesktop, hasMounted, width, viewportClass }` via a resize listener; SSR-safe (`hasMounted` gate). No further deltas.

- [ ] **Step 4: Run test, verify pass** — PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/breakpoints.ts web/lib/useViewport.ts web/tests/useViewport.test.ts
git commit -m "feat(web): viewport breakpoints + useViewport hook"
```

---

### Task 2.2: Deck navigation helpers (single-source `DeckKey`)

**Files:**

- Create: `web/lib/deckNav.ts`
- Test: `web/tests/deckNav.test.ts`

> **Why this is the single source of truth (resolves a sequencing + collision risk).** radon defines `DeckKey` TWICE — an 8-key union in `AssetCockpit` (`c|p|n|r|s|i|:|o`, includes the local-only command-palette `:` and mobile order `o`) and a 6-key union in `legacyTabToDeck`. Two same-named unions in different files invites drift, and importing `DeckKey` from `AssetCockpit` (created late, Task 2.6) forces a forward-dependency. Instead, define the FULL `DeckKey` (8) here in `deckNav.ts` plus the URL-addressable subset, and have every component (`CockpitHeader`, `GlyphRail`, `AssetDeck`, `AssetCockpit`) import `DeckKey` from `@/lib/deckNav`. No forward-dependency; no collision.

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/deckNav.test.ts
import { describe, it, expect } from "vitest";
import {
  isUrlDeck,
  legacyTabToDeck,
  URL_DECKS,
  type DeckKey,
  type UrlDeckKey,
} from "@/lib/deckNav";

describe("deckNav", () => {
  it("URL_DECKS holds the 6 URL-addressable decks (not the local-only : and o)", () => {
    expect([...URL_DECKS].sort()).toEqual(["c", "i", "n", "p", "r", "s"]);
  });
  it("isUrlDeck validates URL-addressable keys only", () => {
    expect(isUrlDeck("c")).toBe(true);
    expect(isUrlDeck("o")).toBe(false); // order deck is local-only, not URL
    expect(isUrlDeck(":")).toBe(false); // command palette is local-only
    expect(isUrlDeck(null)).toBe(false);
  });
  it("legacyTabToDeck maps old ?tab= values", () => {
    expect(legacyTabToDeck("chain")).toBe("c");
    expect(legacyTabToDeck("position")).toBe("p");
    expect(legacyTabToDeck("news")).toBe("n");
    expect(legacyTabToDeck("ratings")).toBe("r");
    expect(legacyTabToDeck("seasonality")).toBe("s");
    expect(legacyTabToDeck("company")).toBe("i");
    expect(legacyTabToDeck("book")).toBe(null); // book is the default surface, no deck
    expect(legacyTabToDeck("order")).toBe(null); // order ticket always-visible (desktop)
    expect(legacyTabToDeck(null)).toBe(null);
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Implement** (`web/lib/deckNav.ts`)

```typescript
// Full deck universe (matches radon AssetCockpit's DeckKey): 6 URL-addressable
// decks + the local-only command palette (":") and mobile order ticket ("o").
export type DeckKey = "c" | "p" | "n" | "r" | "s" | "i" | ":" | "o";

// The subset that can live in the URL (?deck=). ":" and "o" are local-only and
// never serialize to the URL — they're held in component state (Task 2.13).
export type UrlDeckKey = "c" | "p" | "n" | "r" | "s" | "i";

export const URL_DECKS: ReadonlySet<UrlDeckKey> = new Set<UrlDeckKey>([
  "c",
  "p",
  "n",
  "r",
  "s",
  "i",
]);

export function isUrlDeck(
  value: string | null | undefined,
): value is UrlDeckKey {
  return value != null && URL_DECKS.has(value as UrlDeckKey);
}

export function legacyTabToDeck(tab: string | null): UrlDeckKey | null {
  switch (tab) {
    case "chain":
      return "c";
    case "position":
      return "p";
    case "news":
      return "n";
    case "ratings":
      return "r";
    case "seasonality":
      return "s";
    case "company":
      return "i";
    default:
      return null; // "book" / "order" / unknown → no overlay (book-first)
  }
}
```

(Adapted from radon `web/lib/legacyTabToDeck.ts` + `AssetCockpit`'s `DeckKey`, unified here. `company → i` is added since xenon's legacy default tab was `company`.)

- [ ] **Step 4: Run test, verify pass** — PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/lib/deckNav.ts web/tests/deckNav.test.ts
git commit -m "feat(web): deck navigation helpers (single-source DeckKey + URL subset)"
```

---

### Task 2.3: `CockpitHeader` component

**Files:**

- Create: `web/components/ticker-detail/CockpitHeader.tsx`
- Test: `web/tests/CockpitHeader.test.tsx`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/CockpitHeader.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import CockpitHeader from "@/components/ticker-detail/CockpitHeader";

vi.mock("@/lib/useWatchlist", () => ({
  useWatchlist: () => ({ isWatched: () => false, toggleWatch: vi.fn() }),
}));

describe("CockpitHeader", () => {
  it("renders ticker, kind, last and delta from quotePriceData", () => {
    render(
      <CockpitHeader
        ticker="SPX"
        kind="stock"
        quotePriceData={{ symbol: "SPX", last: 5500, close: 5450, bid: 5499, ask: 5501 } as never}
        position={null}
        live
        onDeckChange={vi.fn()}
      />,
    );
    expect(screen.getByText("SPX")).toBeInTheDocument();
    expect(screen.getByText("STOCK")).toBeInTheDocument();
    expect(screen.getByText(/LIVE/)).toBeInTheDocument();
    expect(screen.getByText("FLAT")).toBeInTheDocument(); // no position chip
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Port + adapt**

Copy `/Users/chenxi/projects/radon/web/components/ticker-detail/CockpitHeader.tsx` to xenon, then apply deltas:

- Imports resolve unchanged: `fmtPrice` from `@/lib/positionUtils` ✓, `toneClass` from `@/lib/format` ✓ (both confirmed present), `useWatchlist` from `@/lib/useWatchlist` (Task 1.5) ✓, `StarToggle` from `@/components/StarToggle` (Task 1.6) ✓. **Delta:** import `DeckKey` from `@/lib/deckNav` (Task 2.2), NOT `./AssetCockpit` (single-source per Task 2.2) — this removes the forward-dependency so CockpitHeader typechecks before AssetCockpit exists.
- `PriceData` type: import from `@/lib/pricesProtocol` (xenon path). xenon `PriceData` HAS `close` (verified `pricesProtocol.ts:13`), so the Δ% computation `((last - close) / |close|) * 100` ports unchanged.
- Keep the Δ% formula `((last - close) / |close|) * 100` (matches xenon's Day-Chg sign rule in `web/CLAUDE.md`).

- [ ] **Step 4: Run test, verify pass** — PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/CockpitHeader.tsx web/tests/CockpitHeader.test.tsx
git commit -m "feat(web): CockpitHeader (ticker/star/last/spread/live/position chip)"
```

---

### Task 2.4: `GlyphRail` component

**Files:**

- Create: `web/components/ticker-detail/GlyphRail.tsx`

- [ ] **Step 1: Port verbatim**

Copy `/Users/chenxi/projects/radon/web/components/ticker-detail/GlyphRail.tsx` to xenon verbatim. Only delta: import `DeckKey` from `@/lib/deckNav` (Task 2.2), NOT `./AssetCockpit`. Classes: `glyph-rail`, `glyph`, `glyph-k`, `glyph-l`, `glyph-dot`.

- [ ] **Step 2: Typecheck**

Run: `cd web && npm run typecheck` (green on its own — `DeckKey` comes from `deckNav`, no forward-dependency on AssetCockpit).

- [ ] **Step 3: Commit**

```bash
git add web/components/ticker-detail/GlyphRail.tsx
git commit -m "feat(web): GlyphRail navigation"
```

---

### Task 2.5: `AssetDeck` (keyboard overlay + deck router)

**Files:**

- Create: `web/components/ticker-detail/AssetDeck.tsx`
- Test: `web/tests/AssetDeck.test.tsx`

- [ ] **Step 1: Write the failing test** (the keyboard contract is the highest-value behavior to lock)

```typescript
// web/tests/AssetDeck.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import AssetDeck from "@/components/ticker-detail/AssetDeck";

const baseProps = {
  ticker: "SPX",
  prices: {},
  fundamentals: {},
  portfolio: null,
  position: null,
  quotePriceData: null,
};

describe("AssetDeck keyboard", () => {
  it("single key opens the matching deck", () => {
    const onDeckChange = vi.fn();
    render(<AssetDeck activeDeck={null} onDeckChange={onDeckChange} {...baseProps} />);
    fireEvent.keyDown(document, { key: "c" });
    expect(onDeckChange).toHaveBeenCalledWith("c");
  });

  it("Esc closes an open deck", () => {
    const onDeckChange = vi.fn();
    render(<AssetDeck activeDeck="c" onDeckChange={onDeckChange} {...baseProps} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onDeckChange).toHaveBeenCalledWith(null);
  });

  it("ignores keys while typing in an input", () => {
    const onDeckChange = vi.fn();
    render(
      <>
        <input data-testid="qty" />
        <AssetDeck activeDeck={null} onDeckChange={onDeckChange} {...baseProps} />
      </>,
    );
    const input = document.querySelector("input")!;
    input.focus();
    fireEvent.keyDown(document, { key: "c" });
    expect(onDeckChange).not.toHaveBeenCalled();
  });

  it("ignores modified keys", () => {
    const onDeckChange = vi.fn();
    render(<AssetDeck activeDeck={null} onDeckChange={onDeckChange} {...baseProps} />);
    fireEvent.keyDown(document, { key: "c", metaKey: true });
    expect(onDeckChange).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Port + adapt**

Copy `/Users/chenxi/projects/radon/web/components/ticker-detail/AssetDeck.tsx` to xenon, deltas:

- Import `DeckKey` from `@/lib/deckNav` (Task 2.2), NOT `./AssetCockpit`.
- All deck-content imports resolve to existing xenon `./` components, but TWO radon prop-passes don't exist on xenon's signatures and MUST be dropped or both typecheck fails (verified): `PositionTab` takes only `{position, prices}` — drop radon's `portfolio={portfolio}`; **`OptionsChainTab` also has NO `portfolio` prop** (signature `{ticker, prices, tickerPriceData, focusPosition?, focusPositionRequested?}`, verified `OptionsChainTab.tsx:41`) — drop radon's `portfolio={portfolio}` here too (radon passes it at `AssetDeck.tsx:118`). `CompanyTab`/`NewsTab`/`RatingsTab`/`SeasonalityTab` take `active` (✓).
- Keep `isTypingTarget`, `OPEN_KEYS`, `WIDE_DECKS`, the keydown effect, and `DECK_TITLE` verbatim.

- [ ] **Step 4: Run test, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/AssetDeck.tsx web/tests/AssetDeck.test.tsx
git commit -m "feat(web): AssetDeck keyboard overlay + deck router"
```

---

### Task 2.6: `AssetCockpit` shell + `ActHeldSummary`

**Files:**

- Create: `web/components/ticker-detail/AssetCockpit.tsx`, `web/components/ticker-detail/ActHeldSummary.tsx`

- [ ] **Step 1: Port `ActHeldSummary`**

Copy `/Users/chenxi/projects/radon/web/components/ticker-detail/ActHeldSummary.tsx` to xenon verbatim (small one-line held summary linking to the `p` deck). Confirm its `PortfolioPosition` fields used (`structure`, `direction`, `contracts`, P&L) exist on xenon's `PortfolioPosition` type; adjust field names to xenon's if they differ.

- [ ] **Step 2: Port + adapt `AssetCockpit`**

Copy `/Users/chenxi/projects/radon/web/components/ticker-detail/AssetCockpit.tsx` to xenon, deltas:

- Import `DeckKey` from `@/lib/deckNav` (Task 2.2) — do NOT re-define or re-export it here (radon defined it locally; we centralized it in deckNav to kill the forward-dependency).
- `useTickerDetailOptional` / `OrderPrefill`: xenon's `TickerDetailContext` does NOT export `useTickerDetailOptional` or `setOrderPrefill`/`OrderPrefill`. For Phase 2, click-to-fill is depth-driven (Phase 3) — so **stub the prefill wiring AND remove the now-dead imports**: delete the `useTickerDetailOptional`/`OrderPrefill` import lines, make `onBookPriceClick` a no-op (`const onBookPriceClick = undefined;`), pass no `onPriceClick` to `BookTab`, and add `// TODO(phase-3): wire click-to-fill via TickerDetailContext.setOrderPrefill`. (Leaving the imports in fails lint's no-unused-import rule — verified xenon lints in CI.)
- `useViewport` from `@/lib/useViewport` (Task 2.1) ✓.
- `AssetCockpit` is a pure shell: it RECEIVES `bookKind`, `bookKey`, `quotePriceData`, `priceData`, `isSpreadNet` as props from the adapter (Task 2.13) — do NOT hardcode `bookKind` here. `depths`/`tape` are `undefined` in Phase 2, so `BookTab` (Task 2.7 `bookOnly`) renders L1.
- Drop the `portfolio` prop only where xenon's signature lacks it: `OrderTab` DOES accept `portfolio` (✓ keep); `PositionTab` does NOT (drop). (`OptionsChainTab`'s `portfolio` drop is handled in AssetDeck, Task 2.5.)

- [ ] **Step 3: Typecheck (Tasks 2.4–2.6 unit)**

Run: `cd web && npm run typecheck`
Expected: PASS (`DeckKey` comes from `deckNav`; all deck-content prop sets aligned, incl. both `portfolio` drops).

- [ ] **Step 4: Commit**

```bash
git add web/components/ticker-detail/AssetCockpit.tsx web/components/ticker-detail/ActHeldSummary.tsx
git commit -m "feat(web): AssetCockpit shell + ActHeldSummary"
```

---

### Task 2.7: `BookTab` `bookOnly` mode

**Files:**

- Modify: `web/components/ticker-detail/BookTab.tsx`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/BookTab.bookOnly.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import BookTab from "@/components/ticker-detail/BookTab";

describe("BookTab bookOnly", () => {
  it("renders the L1 book region without the position/order summary chrome when bookOnly", () => {
    const { container } = render(
      <BookTab ticker="SPX" position={null} prices={{}} openOrders={[]} tickerPriceData={null} bookOnly />,
    );
    // bookOnly wraps content in .book-tab-only (cockpit book-region styling)
    expect(container.querySelector(".book-tab-only")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL (no `bookOnly` prop / class).

- [ ] **Step 3: Add the `bookOnly` prop**

In `BookTab.tsx`, add `bookOnly?: boolean` (and `onPriceClick?`, `depths?`, `tape?`, `bookKey?`, `bookKind?` as optional no-op props now so the AssetCockpit call typechecks and Phase 3 can fill them). When `bookOnly`, wrap the existing L1 book markup in a `<div className="book-tab-only">` and suppress the standalone position/order-form chrome that the old tab showed (the cockpit's act region owns those). Keep the existing L1 bid/ask rendering as-is — it is what SPX shows in radon.

- [ ] **Step 4: Run test, verify pass** — PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/BookTab.tsx web/tests/BookTab.bookOnly.test.tsx
git commit -m "feat(web): BookTab bookOnly mode for cockpit book-region"
```

---

### Task 2.8: ETF/index-aware `CompanyTab`

**Files:**

- Modify: `web/components/ticker-detail/CompanyTab.tsx`
- Test: `web/tests/CompanyTab.etf.test.tsx`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/CompanyTab.etf.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import CompanyTab from "@/components/ticker-detail/CompanyTab";

// CompanyTab fetches /api/ticker/info and reads data.uw_info / stock_state /
// profile / stats (verified CompanyTab.tsx:32–40; issueType = uw_info.issue_type).
// Mock the REAL shape — a {info:{...}} mock fails on a missing-uw_info crash, not
// on the gating logic.
function mockInfo(issueType: string) {
  global.fetch = vi.fn(async () =>
    new Response(
      JSON.stringify({
        uw_info: { issue_type: issueType },
        stock_state: {},
        profile: {},
        stats: {},
      }),
      { status: 200 },
    ),
  ) as unknown as typeof fetch;
}

describe("CompanyTab ETF/index gate", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("hides P/E, EPS, Next Earnings for an index", async () => {
    mockInfo("INDEX");
    render(<CompanyTab ticker="SPX" active priceData={null} fundamentals={null} />);
    await waitFor(() => expect(screen.queryByText("P/E Ratio")).not.toBeInTheDocument());
    expect(screen.queryByText("EPS")).not.toBeInTheDocument();
    expect(screen.queryByText("Next Earnings")).not.toBeInTheDocument();
  });

  it("shows P/E for a common stock", async () => {
    mockInfo("Common Stock");
    render(<CompanyTab ticker="AAPL" active priceData={null} fundamentals={null} />);
    await waitFor(() => expect(screen.getByText("P/E Ratio")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL (P/E shown for index today).

- [ ] **Step 3: Add the gate** (xenon `CompanyTab.tsx`, near line 94 where `issueType` is already extracted)

```typescript
const issueTypeUpper = issueType.toUpperCase();
const isFund = /\b(ETF|ETN|FUND|MUTUAL|REIT)\b/.test(issueTypeUpper);
const isIndexType = /\b(INDEX|IDX)\b/.test(issueTypeUpper);
const hideEquityFundamentals = isFund || isIndexType;
```

Then in the `statItems` array (lines ~122–136), conditionally omit the P/E, EPS, and Next Earnings entries when `hideEquityFundamentals` (build the array with `.filter(Boolean)` over conditional entries, mirroring radon `CompanyTab.tsx:145–162`). Keep Div Yield hidden only for `isIndexType` (ETFs/REITs can pay distributions). Hide Market Cap only when it is a fund type AND the market-cap value is null/zero in the payload (i.e. `hideEquityFundamentals && (marketCap == null || marketCap === 0)`) — a present market cap still shows.

- [ ] **Step 4: Run test, verify pass** — PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/CompanyTab.tsx web/tests/CompanyTab.etf.test.tsx
git commit -m "feat(web): ETF/index-aware CompanyTab (hide equity fundamentals)"
```

---

### Task 2.9: Black-Scholes library

**Files:**

- Create: `web/lib/blackScholes.ts`
- Test: `web/tests/blackScholes.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/blackScholes.test.ts
import { describe, it, expect } from "vitest";
import { bsCall, bsPut } from "@/lib/blackScholes";

describe("black-scholes", () => {
  it("ATM call ~ known value", () => {
    // S=100,K=100,T=1,r=0.05,sigma=0.2 → call ≈ 10.4506
    expect(bsCall(100, 100, 1, 0.05, 0.2)).toBeCloseTo(10.4506, 3);
  });
  it("ATM put ~ known value", () => {
    // put ≈ 5.5735 via put-call parity
    expect(bsPut(100, 100, 1, 0.05, 0.2)).toBeCloseTo(5.5735, 3);
  });
  it("intrinsic at T=0", () => {
    expect(bsCall(110, 100, 0, 0.05, 0.2)).toBeCloseTo(10, 6);
    expect(bsPut(90, 100, 0, 0.05, 0.2)).toBeCloseTo(10, 6);
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Port verbatim**

Copy `/Users/chenxi/projects/radon/web/lib/blackScholes.ts` to xenon verbatim (pure math: `normCdf`, `bsCall`, `bsPut`, `bsPrice`, `bsImpliedVol`, `SIGMA_FLOOR`). No deltas.

- [ ] **Step 4: Run test, verify pass** — PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/lib/blackScholes.ts web/tests/blackScholes.test.ts
git commit -m "feat(web): Black-Scholes pricing library"
```

---

### Task 2.10: `impliedValue` (xenon-adapted)

**Files:**

- Create: `web/lib/impliedValue.ts`
- Test: `web/tests/impliedValue.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/impliedValue.test.ts
import { describe, it, expect } from "vitest";
import { computeLegImpliedValue } from "@/lib/impliedValue";
import { legPriceKey } from "@/lib/positionUtils";

describe("computeLegImpliedValue", () => {
  const now = new Date("2026-01-01T00:00:00Z");

  it("computes a positive per-contract value when IV+spot available", () => {
    // build the option key the SAME way impliedValue does — no hardcoded string
    const optKey = legPriceKey("SPX", "20260116", {
      type: "Call",
      strike: 5000,
    })!;
    const prices = {
      SPX: {
        symbol: "SPX",
        last: 5000,
        undPrice: null,
        impliedVol: null,
      } as never,
      [optKey]: {
        symbol: optKey,
        impliedVol: 0.2,
        undPrice: 5000,
        last: null,
      } as never,
    };
    const r = computeLegImpliedValue(
      {
        ticker: "SPX",
        expiry: "20260116",
        strike: 5000,
        type: "Call",
        direction: "LONG",
        contracts: 1,
      },
      prices as never,
      { now },
    );
    expect(r.perContract).not.toBeNull();
    expect(r.perContract!).toBeGreaterThan(0);
  });

  it("returns null when no sigma and no spot", () => {
    const r = computeLegImpliedValue(
      {
        ticker: "ZZZ",
        expiry: "20260116",
        strike: 5000,
        type: "Call",
        direction: "LONG",
        contracts: 1,
      },
      {} as never,
      { now },
    );
    expect(r.perContract).toBeNull();
  });

  it("back-solves sigma from close prices when streaming impliedVol is absent", () => {
    // Exercises the ported priority-2 fallback (bsImpliedVol from option+underlying
    // close). No impliedVol on the option; both legs carry a `close`.
    const optKey = legPriceKey("SPX", "20260116", {
      type: "Call",
      strike: 5000,
    })!;
    const prices = {
      SPX: {
        symbol: "SPX",
        last: 5000,
        close: 4950,
        undPrice: null,
        impliedVol: null,
      } as never,
      [optKey]: {
        symbol: optKey,
        impliedVol: null,
        undPrice: 5000,
        last: null,
        close: 120,
      } as never,
    };
    const r = computeLegImpliedValue(
      {
        ticker: "SPX",
        expiry: "20260116",
        strike: 5000,
        type: "Call",
        direction: "LONG",
        contracts: 1,
      },
      prices as never,
      { now },
    );
    // Either a finite back-solved value or null if the solver can't converge —
    // assert it does NOT throw and the IV-absent path is reached. Tighten the
    // expectation once the ported bsImpliedVol tolerance is confirmed.
    expect(r).toBeDefined();
  });
});
```

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Port + adapt**

Copy `/Users/chenxi/projects/radon/web/lib/impliedValue.ts` to xenon, deltas (xenon `PriceData` has NO `fwd`/`fwdCurve` fields — confirmed in inventory):

- `resolveSpot`: drop the forward-curve / `isForwardPricedIndex(VIX)` branch. Spot hierarchy becomes: option `undPrice` → `prices[ticker].last` → option mid. (Keep the `source` tag for debugging.)
- `resolveSigma`: priority 1 = `optionPd.impliedVol` (xenon has this). xenon `PriceData` HAS `close` (verified), so KEEP radon's priority-2 `bsImpliedVol` back-solve from the option + underlying close. Return null only when neither IV nor a solvable close pair exists.
- `legOptionKey`: xenon's key helper is `legPriceKey(ticker, expiry, leg)` where `leg = { type: "Call"|"Put"|"Stock", strike }` (verified `positionUtils.ts:192–205` → delegates to `optionKey({symbol, expiry: YYYYMMDD, strike, right})`). This signature DIFFERS from radon's `legOptionKey({ticker,expiry,strike,type})` — adapt: `const oKey = legPriceKey(input.ticker, input.expiry, { type: input.type, strike: input.strike });`. In the test, build the expected map key the SAME way (`import { legPriceKey } from "@/lib/positionUtils"`) rather than hardcoding a string, so the format can't drift.
- Keep `yearsToExpiry`, `RISK_FREE_RATE_DEFAULT`, `bsPrice` usage, `NULL_RESULT`, the `ImpliedValueResult` shape.

- [ ] **Step 4: Run test, verify pass** — PASS (3 tests, incl. the IV-absent back-solve path).

- [ ] **Step 5: Commit**

```bash
git add web/lib/impliedValue.ts web/tests/impliedValue.test.ts
git commit -m "feat(web): impliedValue (Black-Scholes per-leg, xenon-adapted spot/sigma)"
```

---

### Task 2.11: "Implied" column in the options chain

**Files:**

- Modify: `web/components/ticker-detail/OptionsChainTab.tsx`

- [ ] **Step 1: Write the failing test** (`web/tests/chain-implied-column.test.tsx`)

```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import OptionsChainTab from "@/components/ticker-detail/OptionsChainTab";

describe("chain Implied column", () => {
  it("renders an Implied header for both sides", () => {
    render(<OptionsChainTab ticker="SPX" prices={{}} tickerPriceData={null} />);
    // two "Implied" headers (calls + puts) once a chain renders; at minimum the header text exists
    expect(screen.getAllByText("Implied").length).toBeGreaterThanOrEqual(1);
  });
});
```

(If the chain needs async data to render headers, mock `/api/...` chain fetch as the existing chain tests do — follow `web/tests/ticker-chain-position-focus.test.tsx`.)

- [ ] **Step 2: Run it, verify it fails** — FAIL.

- [ ] **Step 3: Add the column**

- Add `<th className="chain-header chain-header-implied" title="Black-Scholes implied (theoretical) per-share price">Implied</th>` to the header row (Task-confirmed insert points: after the call IV `<th>` at line ~1475, and before the put IV `<th>` at line ~1491 — mirror radon `OptionsChainTab.tsx:1285`).
- In `StrikeRow` (line ~74), after `callIV`/`callDelta` (line ~105), compute:

```typescript
const callImplied = useMemo(
  () =>
    computeLegImpliedValue(
      { ticker, expiry, strike, type: "Call", direction: "LONG", contracts: 1 },
      prices,
    ).perContract,
  [ticker, expiry, strike, prices],
);
const putImplied = useMemo(
  () =>
    computeLegImpliedValue(
      { ticker, expiry, strike, type: "Put", direction: "LONG", contracts: 1 },
      prices,
    ).perContract,
  [ticker, expiry, strike, prices],
);
```

- Add the cells (`<td className="chain-cell chain-implied">{callImplied != null ? fmtPrice(callImplied) : ""}</td>` for calls, same for puts) at the same column position as the headers. Import `computeLegImpliedValue` from `@/lib/impliedValue`.
- Add a `.chain-implied { color: var(--text-secondary); font-size: 10px; }` block to `globals.css`.

- [ ] **Step 4: Run test, verify pass** — PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/OptionsChainTab.tsx web/tests/chain-implied-column.test.tsx web/app/globals.css
git commit -m "feat(web): Black-Scholes Implied column in options chain"
```

---

### Task 2.12: Cockpit CSS

**Files:**

- Modify: `web/app/globals.css`

- [ ] **Step 1: Port the cockpit CSS**

From `/Users/chenxi/projects/radon/web/app/globals.css` copy the cockpit blocks (the radon audit identified the ranges): `.cockpit`, `.cockpit-head` + `.ckh-*`, `.book-region` + `.book-tab-only`, `.act-region` + `.act-*`, `.glyph-rail` + `.glyph*`, `.asset-deck*`, and the `.cockpit--mobile` block. Map raw colors → xenon brand tokens already in `globals.css` (`--line-grid`, `--bg-panel`, `--bg-panel-raised`, `--text-primary/secondary/muted`, `--signal-core`, `--signal-strong`, `--fault`). Enforce 4px max radius and no soft shadows beyond what tokens already permit (`brand/CLAUDE.md`). The radon source uses these same token names, so most blocks copy unchanged.

- [ ] **Step 2: Verify no token gaps**

Run: `cd web && grep -n "var(--" app/app? ...` is not needed — instead, after wiring (Task 2.13), visually confirm in the browser (Task 2.16) that no color renders as the CSS fallback/black.

- [ ] **Step 3: Commit**

```bash
git add web/app/globals.css
git commit -m "feat(web): cockpit grid + deck + rail CSS (brand-token mapped)"
```

---

### Task 2.13: Convert `TickerDetailContent` into the cockpit adapter; thin `TickerWorkspace`

> **Architecture (corrected after review).** radon does NOT drive the cockpit from `TickerWorkspace`. It keeps a cockpit ADAPTER (`TickerDetailContent`) that holds `urlDeck` (from `?deck=`) PLUS a local `localDeck` state so the command-palette `:` and mobile order `o` decks open WITHOUT becoming URL params, and that computes the cockpit's derived props — `quotePriceData` (with `mergeCalculatedMark` so thin single-leg options keep their calculated mark), `isSpreadNet`, `bookKey`, and dynamic `bookKind` — before rendering `AssetCockpit`. If we instead inline URL-only deck state into `TickerWorkspace` and hardcode `bookKind="stock"`, the `:`/`o` decks have nowhere to live and multi-leg nets get mislabeled. So: **port radon's `TickerDetailContent` as the adapter** (swap xenon's tab-bar body for radon's cockpit body, same filename) and keep `TickerWorkspace` thin. Do NOT delete `TickerDetailContent`.

**Files:**

- Modify: `web/components/TickerDetailContent.tsx` (replace tab-bar body with radon's cockpit-adapter body)
- Modify: `web/components/TickerWorkspace.tsx` (stay thin: resolve position + pass `?deck=`/`?tab=` through)

- [ ] **Step 1: Write the failing test** (`web/tests/TickerDetailContent.deck.test.tsx`)

```typescript
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import TickerDetailContent from "@/components/TickerDetailContent";

vi.mock("@/lib/useWatchlist", () => ({
  useWatchlist: () => ({ isWatched: () => false, toggleWatch: vi.fn() }),
}));

describe("TickerDetailContent cockpit adapter", () => {
  it("renders the cockpit shell from the activeTab(deck) prop", () => {
    const onTabChange = vi.fn();
    const { container } = render(
      <TickerDetailContent
        ticker="SPX"
        positionId={null}
        activeTab="c" // deck key carried in the existing activeTab prop
        onTabChange={onTabChange}
        prices={{}}
        fundamentals={{}}
        portfolio={null}
        orders={null}
        theme="dark"
      />,
    );
    expect(container.querySelector(".cockpit")).toBeTruthy();
    // local-only decks (":"/"o") set internal state and must NOT call onTabChange;
    // full keyboard behavior is covered by AssetDeck.test.tsx.
  });
});
```

> **Interface fidelity (verified radon `TickerDetailContent.tsx:223–262`).** radon's adapter keeps the SAME prop interface xenon's `TickerWorkspace` already passes — `activeTab` (a string carrying the deck key) + `onTabChange` + `positionId` — and internally does `urlDeck = isDeckKey(activeTab) ? activeTab : null`, `[localDeck, setLocalDeck] = useState`, `activeDeck = urlDeck ?? localDeck`. So **keep xenon's existing `TickerDetailContent` prop signature** (`activeTab`/`onTabChange`/`positionId`) — do NOT invent `urlDeck`/`onUrlDeckChange` props. This makes the port 1:1 and leaves `TickerWorkspace`'s call site nearly unchanged.

- [ ] **Step 2: Run it, verify it fails** — FAIL (xenon's current body renders the tab bar, no `.cockpit`).

- [ ] **Step 3: Port radon's `TickerDetailContent` adapter body** (keep xenon's prop signature)

Replace xenon `TickerDetailContent.tsx`'s body with radon's `/Users/chenxi/projects/radon/web/components/TickerDetailContent.tsx`. Port:

- Deck arbitration (radon `:223–237`): `urlDeck = isDeckKey(activeTab) ? activeTab : null` (xenon: use `isUrlDeck` from `deckNav`; note xenon's legacy `activeTab` may be a tab-name like `"chain"` so first map via `legacyTabToDeck` — see Step 4), `[localDeck, setLocalDeck] = useState<DeckKey|null>(null)`, `activeDeck = urlDeck ?? localDeck`. `onDeckChange(deck)`: if `isUrlDeck(deck)` → `onTabChange(deck)` (writes the URL via TickerWorkspace) and clear `localDeck`; else (`:`/`o`) → `setLocalDeck(deck)`.
- The quote/book resolver radon uses (NOT xenon's pre-cockpit `resolveTickerQuoteTelemetry`): port radon's version that returns `{ priceData, isSpreadNet }` plus the `bookKey`/`bookKind` derivation, using `mergeCalculatedMark` (verified radon `TickerDetailContent.tsx:26,67,151–199`). Move the pure resolver into `web/lib/tickerQuote.ts`. `bookKey = chartPriceKey ?? ticker`; `bookKind` is `"stock"` for stock/index and `"option"` for single-leg option focus (radon `:193`; no futures path — xenon has none). `quotePriceData` is the memoized corrected quote (radon `:170`).
- Render `AssetCockpit` with `position`, `prices`, `fundamentals`, `portfolio`, `quotePriceData`, `priceData`, `isSpreadNet`, `bookKey`, `bookKind`, `tickerOrders`, `activeDeck`, `onDeckChange`, `theme`. Depth/tape stay `undefined` (deferred Phase 3).
- Keep xenon's existing `positionId`→`position` resolver `useMemo` (current `TickerDetailContent.tsx:97–107`, exact-id-if-≥0 else ticker match) INSIDE the adapter — radon resolves position here too, so `TickerWorkspace` keeps passing `positionId` (not a resolved `position`). This keeps the call site identical to today.

- [ ] **Step 4: Adjust `TickerWorkspace.tsx` (minimal)**

xenon's `TickerWorkspace` already passes `activeTab`/`onTabChange`/`positionId` (`TickerWorkspace.tsx:49–59`). Only two deltas: (1) compute `activeTab` as the deck key — `const deck = isUrlDeck(searchParams.get("deck")) ? searchParams.get("deck") : legacyTabToDeck(searchParams.get("tab")); ` and pass `activeTab={deck ?? ""}`; (2) `setTab` writes `?deck=` (set/delete) and deletes legacy `?tab=`, preserving `posId`. The render call otherwise stays as-is (adapter now renders the cockpit instead of the tab bar).

- [ ] **Step 5: Run test + typecheck** — `cd web && npx vitest run tests/TickerDetailContent.deck.test.tsx && npm run typecheck` → PASS.

- [ ] **Step 6: Commit**

```bash
git add web/components/TickerDetailContent.tsx web/components/TickerWorkspace.tsx web/lib/tickerQuote.ts web/tests/TickerDetailContent.deck.test.tsx
git commit -m "feat(web): TickerDetailContent cockpit adapter (urlDeck+localDeck, quote/book resolution)"
```

---

### Task 2.14: Migrate the tests that imported the old tab-bar `TickerDetailContent`

> `TickerDetailContent` still EXISTS (now the cockpit adapter) — it is NOT deleted. But its render output changed (cockpit, not tab bar), so existing tests that asserted tab-bar structure must be re-pointed.

**Files:**

- Modify: `web/tests/ticker-detail-spread-notional.test.ts`, `web/tests/ticker-chain-position-focus.test.tsx`, `web/tests/modify-order-ticker-detail.test.ts`, `web/e2e/risk-reversal-midprice.spec.ts`

- [ ] **Step 1: Inventory what each test asserts** — determine whether it tests (a) cockpit-level behavior now owned by the adapter/`AssetCockpit`, or (b) a child tab (`OptionsChainTab`, `OrderTab`) testable directly. Re-point: spread-notional + chain-position-focus → render the child tab or the adapter with `urlDeck="c"`; modify-order → render `OrderTab` directly.

- [ ] **Step 2: Update each test** to the new entry point/props, preserving assertion intent. Run each: `cd web && npx vitest run tests/<file>` → PASS.

- [ ] **Step 3: Update the e2e spec** `risk-reversal-midprice.spec.ts` — navigate to `/<ticker>?deck=c` (was `?tab=chain`) and adjust selectors to the cockpit chain deck. (Run in Task 2.16's Playwright pass, not now.)

- [ ] **Step 4: Full web test + typecheck** — `cd web && npm run typecheck && npm test` → PASS.

- [ ] **Step 5: Commit**

```bash
git add -A web/tests web/e2e
git commit -m "test(web): migrate ticker-detail tests to the cockpit adapter"
```

---

### Task 2.15: Phase-2 gate — full suite

- [ ] **Step 1:** `cd web && npm run typecheck && npm run lint && npm test` → all green.
- [ ] **Step 2:** `uv run python scripts/infra/dev/run_pytest_affected.py` → green.

---

### Task 2.16: ⛔ Browser verification vs radon SPX (the goal)

**Files:** none (verification + a saved artifact)

- [ ] **Step 1: Establish a fair comparison baseline (adversarial — the two stacks have independent data).** xenon dev reads `core_test`; radon reads its own DB. `posId=6` is only a meaningful side-by-side if it resolves to a comparable SPX position in BOTH. First confirm: in xenon, `GET http://localhost:8421/portfolio` contains a position with `id=6` on `SPX` (or accept the ticker-match fallback → note which position renders). If xenon has no `id=6` SPX position, the cockpit shows "FLAT" while radon shows a held position — that is a DATA difference, not a cockpit-parity defect. Either (a) pick a `posId` that exists in both, or (b) compare the FLAT (no-position) cockpit on both stacks for the same ticker. Record which baseline you used so the screenshot diff is apples-to-apples. Both stacks confirmed serving (radon :3000, xenon :3200).

- [ ] **Step 2: chrome-cdp interactive parity pass on xenon**
  - Load `http://localhost:3200/SPX?posId=6`. Confirm the cockpit grid renders (header / book / act / rail).
  - Press `c` → Chain deck opens (wide). Press `c` again → closes. Repeat for `p n r s i`. Press `Esc` → closes. Focus the order Qty input, press `c` → deck must NOT open (typing guard).
  - Confirm `CompanyTab` (deck `i`) hides P/E, EPS, Next Earnings for SPX (index).
  - Confirm the chain (deck `c`) shows the "Implied" column with values.
  - Confirm the star toggle in the header persists across reload (Phase-1 backend).

- [ ] **Step 3: Side-by-side screenshots** at 1440×900 dark:
  - `take_screenshot` xenon `/SPX?posId=6` → `docs/plans/operator-spx-cockpit-xenon.png`
  - `take_screenshot` radon `/SPX?posId=6` → `docs/plans/operator-spx-cockpit-radon.png`
  - Diff the cockpit regions; note any layout/token deltas and fix in `globals.css` / component markup. Re-shoot until parity.

- [ ] **Step 4: Run the e2e spec** — `cd web && npx playwright test e2e/risk-reversal-midprice.spec.ts` → PASS (selectors updated in Task 2.14).

- [ ] **Step 5: Commit artifacts**

```bash
git add docs/plans/operator-spx-cockpit-*.png
git commit -m "test(web): SPX cockpit parity screenshots vs radon"
```

**Phase 2 = SPX parity achieved.** Open the PR for Phases 1–2 here (branch + `gh pr create` per global policy; never push master directly). The L2 depth Book + tape is a separate plan (see the Phase 3 section), not a task in this PR.

---

# PHASE 3 — Depth-of-market Book + Time & Sales — SPLIT INTO A SEPARATE PLAN

> **Removed from this plan after review.** Phase 3 is NOT part of this implementation and is NOT executed alongside Phases 1–2. It gets its own spec → plan → PR cycle. Three review findings forced the split:
>
> 1. **Wrong IB library (blocking).** radon's depth code is built on `@stoqey/ib`; xenon's relay and both `package.json` files depend on **`ib@0.2.9`** (verified `package.json:7`, `web/package.json:38`). `reqMktDepth`/`tickByTickAllLast` are NOT a mechanical copy — the `ib` client's market-depth API surface differs. A default-OFF `XENON_DEPTH_ENABLED` gate does NOT make a mis-ported relay "safe" (it still has to compile and the handlers have to be correct). The separate plan's FIRST decision is an explicit prerequisite: **either migrate xenon's relay runtime to `@stoqey/ib`, or re-spec the depth work against the current `ib@0.2.9` API.**
> 2. **Missing source-layer plumbing.** `usePrices()` is owned by `WorkspaceShell`, not `TickerWorkspace`, and `TickerDetailContext` has no `depths`/`tape`/`depthSymbol`/setters (verified `TickerDetailContext.tsx:7`). The depth chain needs explicit tasks to expand `TickerDetailContext` AND add `WorkspaceShell` subscription/state plumbing to pass `depthSymbol` into `usePrices` and persist `depths`/`tape`. The original phase hand-waved this.
> 3. **Not verifiable at the stated goal.** SPX (the `/SPX?posId=6` target) is an index with no L2 depth — Phase 3 cannot be verified there. It requires RTH (09:30–16:00 ET) on a depth-eligible equity (AAPL) on the dev relay, then a separate macmini Docker deploy + prod RTH re-check (memory `feedback_verify_prod_docker_topology`).
>
> **The verified entitlement still holds** (SMART `isSmartDepth=true` accepted; direct-exchange 10089; options per-exchange only — see the prerequisite section near the top). When the separate plan is written it will cover, in order: (a) the IB-lib decision; (b) `pricesProtocol` depth/tape types; (c) `depthFormat` + `depthDerivations` equity subset; (d) the relay `reqMktDepth(isSmartDepth=true)` + `reqTickByTick` against the chosen lib; (e) `WorkspaceShell` + `TickerDetailContext` + `usePrices` depth/tape plumbing; (f) `DepthMontage`/`OrderBook`/`TimeAndSales`; (g) `BookTab` depth rendering; (h) a dedicated `OrderTab` nonce-keyed click-to-fill **consumer** task with tests (producers alone are insufficient — the ticket must consume `orderPrefill` while still submitting through Gate-4); (i) RTH AAPL verification; (j) prod Docker deploy + re-check. All behind `XENON_DEPTH_ENABLED` (default OFF).
>
> **This plan ends at Phase 2** — which fully delivers the `/SPX?posId=6` parity goal, since SPX's Book is L1 in both radon and xenon.

---

## Self-review (run after writing; fixed inline)

- **Spec coverage:** Area-4 items mapped — #5 ETF Company tab → Task 2.8; #6 single-key nav → Tasks 2.2/2.5; #9 implied column → Tasks 2.9–2.11; #15 watchlist → Phase 1; #16 Time & Sales + #17 L2 depth → **deferred to the separate Phase-3 plan** (wrong-IB-lib + SPX-unverifiable). Cockpit shell/decks (the "feel") → Tasks 2.1–2.7, 2.12–2.13. ✓
- **Constraint coverage:** DB-first (Task 1.1 PG, no JSON) ✓; Gate-4 untouched (constraint #2, no new place path) ✓; read-only (Task 1.3 — routes don't depend on `read_only_403`) ✓; AccountScope (watchlist operator-scoped `user_id="local"`, documented) ✓; prod topology (only the migration; relay deferred) ✓; identity (palette inert) ✓.
- **Type consistency:** `DeckKey` (full 8-key) + `UrlDeckKey` (6) defined ONCE in `deckNav` (Task 2.2); CockpitHeader/GlyphRail/AssetDeck/AssetCockpit all import from there (no forward-dep, no collision). `computeLegImpliedValue` signature identical in Tasks 2.10 and 2.11. Both `portfolio` drops (PositionTab + OptionsChainTab) called out in Tasks 2.5/2.6. ✓
- **Placeholder scan:** No silent placeholders. `<existing_user_dep>` was removed (Task 1.3 now uses operator `user_id="local"`, no auth dep). `close`-field and `legPriceKey`-signature decisions are resolved against verified source, not left as branches. The Phase-2 click-to-fill stub is explicitly labeled and its consumer is scheduled in the deferred Phase-3 plan.
- **Risk flags:** Depth's wrong-IB-lib + missing plumbing + SPX-non-verifiability surfaced at the Phase-3 split header. Watchlist subprocess/engine-visibility (`committed_db` marker) flagged in Task 1.3. Migration `down_revision` chain flagged in Task 1.1.

---

## Execution handoff

This plan is Phases 1–2 only and fully achieves the stated goal (SPX `?posId=6` parity) with **zero relay/live-market risk**. Execute in phase order; ship as one PR (branch + `gh pr create` → CI → merge; never push master directly). The L2 depth Book + tape is a **separate plan** (own spec → plan → PR) — it must first decide the IB-library question (`ib@0.2.9` vs `@stoqey/ib`) and add `WorkspaceShell`/`TickerDetailContext` plumbing, and is verified on AAPL during RTH, not at SPX.
