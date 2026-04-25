# Order Quote ConId GTC Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix single-leg order quote validation by fetching backend quotes on an event-loop-safe IB data worker, forwarding the real contract id through quote/place flows, surfacing quote-fetch failures distinctly, and adding DAY/GTC selection to the position order modal.

**Architecture:** Backend quote resolution stays behind FastAPI `/orders/quote` and the existing `IBPool` data role, but the blocking `ib_insync` work must run in a worker thread that owns an event loop and is serialized by `pool.acquire("data")`. Frontend single-leg flows must derive `conId` from position leg data, so the IB sync output, portfolio schema, and UI types need a small data-contract extension before fixing `OrderTab`. Position modal TIF should reuse the existing `tif` payload convention already accepted by `/api/orders/place`.

**Tech Stack:** FastAPI, `ib_insync`, pytest/TestClient, Next.js App Router, React, Vitest/jsdom, Playwright.

---

## Context And Guardrails

- Worktree: `/Users/chenxi/projects/xenon/.worktrees/order-quote-conid-gtc`
- Follow repo TDD: failing test first, minimal implementation, green, then refactor.
- Python commands must use `uv`; in this sandbox use `UV_CACHE_DIR=/tmp/uv-cache` if needed.
- Frontend commands run under `web/`.
- Do not use Yahoo Finance.
- Do not reintroduce the reverted quote-token architecture beyond the currently existing `/orders/quote` and `/orders/place` quote guard flow.
- UI work requires rendered browser verification after unit tests.

---

### Task 1: Add `conId` To Portfolio Leg Data Contract

**Files:**
- Modify: `src/xenon/execution/ib_sync.py`
- Modify: `web/lib/types.ts`
- Modify: `web/lib/portfolioDataSchema.ts`
- Test: existing Python or web portfolio schema tests if present; otherwise add a focused Vitest schema/type regression in `web/tests/position-order-seed-ticket.test.ts`

**Step 1: Write the failing test**

Add a regression that constructs a single-leg option `PortfolioPosition` with `legs[0].conId = 861001` and verifies downstream ticket/payload seed code preserves access to that id.

Suggested test in `web/tests/position-order-seed-ticket.test.ts`:

```ts
it("keeps the IB conId on single-leg option positions", () => {
  const position = makeSingleCallPosition({ legOverrides: { conId: 861001 } });
  expect(position.legs[0].conId).toBe(861001);
});
```

If the helper does not support overrides, add a minimal local object using `PortfolioPosition`.

**Step 2: Run test to verify it fails**

Run:

```bash
cd web && npm test -- position-order-seed-ticket
```

Expected: TypeScript/Vitest fails because `PortfolioLeg` does not allow `conId`, or the helper cannot assert it yet.

**Step 3: Implement minimal data-contract change**

In `src/xenon/execution/ib_sync.py`, add `conId` to each formatted collapsed leg:

```python
"conId": leg.get("conId"),
```

Place it with the other leg identity fields in the `formatted_legs.append({...})` object.

In `web/lib/types.ts`, extend `PortfolioLeg`:

```ts
conId?: number | null;
```

In `web/lib/portfolioDataSchema.ts`, extend `PortfolioLegSchema`:

```ts
conId: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
```

**Step 4: Run test to verify it passes**

Run:

```bash
cd web && npm test -- position-order-seed-ticket
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py web/lib/types.ts web/lib/portfolioDataSchema.ts web/tests/position-order-seed-ticket.test.ts
git commit -m "fix: carry option conId in portfolio legs"
```

---

### Task 2: Backend `/orders/quote` Event-Loop-Safe IB Snapshot

**Files:**
- Modify: `src/xenon/api/server.py`
- Test: `scripts/tests/test_quote_route.py`

**Step 1: Write the failing backend tests**

Add one test proving the route uses the pool `data` role through `acquire("data")` and one test proving the worker thread has an event loop before the fake IB client is called.

Suggested additions to `scripts/tests/test_quote_route.py`:

```python
def test_quote_route_runs_snapshot_worker_with_event_loop(client, monkeypatch):
    import asyncio
    import threading
    from decimal import Decimal
    from xenon.api import server

    seen = {"loop": False, "thread": None}

    class FakeClient:
        def qualify_contract(self, contract):
            asyncio.get_event_loop()
            seen["loop"] = True
            seen["thread"] = threading.current_thread().name
            return contract

        def get_quote(self, contract, snapshot=True):
            asyncio.get_event_loop()
            return type("Ticker", (), {"bid": 1.2, "ask": 1.3, "bidSize": 4, "askSize": 5})()

    class FakeAcquire:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self, role):
            assert role == "data"
            return FakeAcquire()

    monkeypatch.setattr(server, "ib_pool", FakePool())

    resp = client.get("/orders/quote", params={"ticker": "SPY", "con_id": 756733})

    assert resp.status_code == 200, resp.text
    assert seen["loop"] is True
    assert seen["thread"] != threading.current_thread().name
```

Also keep or adapt the existing monkeypatch seam test so a fake snapshot can return deterministic `Decimal` values.

**Step 2: Run test to verify it fails**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest scripts/tests/test_quote_route.py -xvs
```

Expected before implementation: FAIL because current route uses `asyncio.to_thread(_fetch_quote_snapshot, ...)` against `pool.get("data")` and the raw worker does not set an event loop.

**Step 3: Implement minimal backend fix**

Refactor `src/xenon/api/server.py` around `/orders/quote`:

```python
def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fetch_quote_snapshot_with_client(client: Any, ticker: str, con_id: int) -> dict:
    _ensure_thread_event_loop()
    contract = Contract(conId=int(con_id), exchange="SMART")
    qualified = client.qualify_contract(contract)
    tk = client.get_quote(qualified, snapshot=True)
    return _ticker_to_quote_snapshot(ticker, con_id, tk)
```

Extract the existing bid/ask validation into `_ticker_to_quote_snapshot(...)` so tests can still cover it without real IB.

Change the route to serialize through the data role:

```python
@app.get("/orders/quote")
async def orders_quote(ticker: str, con_id: int):
    secret = os.environ.get("XENON_QUOTE_TOKEN_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="quote secret not configured")
    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")
    try:
        async with pool.acquire("data") as client:
            snap = await asyncio.to_thread(_fetch_quote_snapshot_with_client, client, ticker, con_id)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    ...
```

Keep `_fetch_quote_snapshot(ticker, con_id)` as a test seam if existing tests monkeypatch it, or update tests to patch the new helper. Prefer the new helper only if it simplifies the code.

**Step 4: Run test to verify it passes**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest scripts/tests/test_quote_route.py -xvs
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/xenon/api/server.py scripts/tests/test_quote_route.py
git commit -m "fix: fetch order quotes on event-loop-safe IB worker"
```

---

### Task 3: Stop Single-Leg OrderTab From Requesting `con_id=0`

**Files:**
- Modify: `web/components/ticker-detail/OrderTab.tsx`
- Modify: `web/components/ticker-detail/useQuoteToken.ts`
- Test: add or extend `web/tests/order-tab-reason-toast.test.tsx`
- Test: extend `web/tests/order-payload.test.ts`

**Step 1: Write failing payload-builder test**

Extend `web/tests/order-payload.test.ts` so a single-leg option position with `legs[0].conId = 861001` produces `con_id: 861001`.

```ts
it("includes con_id for single-leg option positions", () => {
  const payload = buildSingleLegOrderPayload({
    ticker: "AAOI",
    action: "SELL",
    quantity: 1,
    limitPrice: 9,
    tif: "DAY",
    position: makeCallPosition({ legOverrides: { conId: 861001 } }),
  });
  expect(payload.con_id).toBe(861001);
});
```

**Step 2: Write failing OrderTab route test**

Add a jsdom test that renders `OrderTab` with a single-leg option position and captures fetch calls.

Expected assertions:

```ts
expect(fetchSpy).toHaveBeenCalledWith(
  expect.stringContaining("/api/orders/quote?ticker=AAOI&con_id=861001"),
  expect.anything(),
);
expect(placeBody.con_id).toBe(861001);
```

Use the existing `order-tab-reason-toast.test.tsx` pattern for rendering and fetch stubbing.

**Step 3: Run tests to verify they fail**

Run:

```bash
cd web && npm test -- order-payload order-tab-reason-toast
```

Expected: FAIL because `OrderTab` currently calls `useQuoteToken({ conId: 0 })` and `buildSingleLegOrderPayload()` does not include `con_id`.

**Step 4: Implement minimal frontend plumbing**

In `OrderTab.tsx`, add a helper:

```ts
function singleLegConId(position: PortfolioPosition | null): number | null {
  if (
    position == null ||
    position.structure_type === "Stock" ||
    position.legs.length !== 1
  ) {
    return null;
  }
  const conId = position.legs[0].conId;
  return typeof conId === "number" && conId > 0 ? conId : null;
}
```

Update `buildSingleLegOrderPayload()` for single-leg options:

```ts
...(typeof leg.conId === "number" && leg.conId > 0 ? { con_id: leg.conId } : {}),
```

Change quote hook call:

```ts
const quoteConId = singleLegConId(position);
const quote = useQuoteToken({
  ticker,
  conId: quoteConId,
  expiry: position?.expiry ?? null,
});
```

In `useQuoteToken.ts`, allow null and skip fetch when unavailable:

```ts
type Options = { ticker: string; conId: number | null; expiry: string | null };
...
if (conId == null || conId <= 0) {
  setToken(null);
  setError(null);
  return;
}
```

**Step 5: Run tests to verify they pass**

Run:

```bash
cd web && npm test -- order-payload order-tab-reason-toast quote-token-client
```

Expected: PASS.

**Step 6: Commit**

```bash
git add web/components/ticker-detail/OrderTab.tsx web/components/ticker-detail/useQuoteToken.ts web/tests/order-payload.test.ts web/tests/order-tab-reason-toast.test.tsx web/tests/quote-token-client.test.ts
git commit -m "fix: use real option conId for single-leg orders"
```

---

### Task 4: Surface Quote-Fetch Failure Distinctly

**Files:**
- Modify: `web/components/ticker-detail/OrderTab.tsx`
- Modify if needed: `web/components/ticker-detail/useQuoteToken.ts`
- Test: `web/tests/order-tab-reason-toast.test.tsx`

**Step 1: Write failing UI test**

Add a test where `/api/orders/quote` returns 503 and `/api/orders/place` must not be called.

```ts
it("surfaces quote fetch failure instead of falling through to STALE_QUOTE", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/orders/quote")) {
      return Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "IB data role unavailable" }) } as Response);
    }
    if (url.includes("/api/orders/place")) {
      throw new Error("place should not be called");
    }
    return quoteOk();
  });

  // render single-leg option OrderTab, enter price, click place/confirm
  // assert visible message includes "Quote unavailable" and upstream detail
  // assert no /api/orders/place call happened
});
```

**Step 2: Run test to verify it fails**

Run:

```bash
cd web && npm test -- order-tab-reason-toast
```

Expected: FAIL because current code submits with `quote.token === null`, causing backend `STALE_QUOTE`.

**Step 3: Implement minimal UI guard**

Improve `useQuoteToken()` error detail:

```ts
if (!res.ok) {
  const body = await res.json().catch(() => null);
  const detail = body && typeof body.detail === "string" ? body.detail : `quote ${res.status}`;
  throw new Error(detail);
}
```

Before placing non-combo single-leg orders in `OrderTab.tsx`, block if the quote fetch failed:

```ts
if (quote.error) {
  setError(`Quote unavailable: ${quote.error}`);
  setLoading(false);
  return;
}
if (payload.type !== "combo" && !quote.token) {
  setError("Quote unavailable: waiting for latest IB quote");
  setLoading(false);
  return;
}
```

Avoid applying this to combo flow because quote-token guard currently skips combos.

**Step 4: Run test to verify it passes**

Run:

```bash
cd web && npm test -- order-tab-reason-toast quote-token-client
```

Expected: PASS.

**Step 5: Commit**

```bash
git add web/components/ticker-detail/OrderTab.tsx web/components/ticker-detail/useQuoteToken.ts web/tests/order-tab-reason-toast.test.tsx web/tests/quote-token-client.test.ts
git commit -m "fix: show quote fetch failures before order submit"
```

---

### Task 5: Add DAY/GTC Control To Position Order Modal

**Files:**
- Modify: `web/components/PositionOrderModal.tsx`
- Test: `web/tests/position-order-modal.test.tsx`

**Step 1: Write failing modal test**

Add test:

```ts
it("submits selected GTC time-in-force", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ orderId: "abc", status: "ok" }),
  });
  (global as any).fetch = fetchMock;

  const { getByRole } = render(
    <PositionOrderModal
      position={stockPos}
      prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
      onClose={() => {}}
    />,
  );

  fireEvent.click(getByRole("button", { name: "GTC" }));
  fireEvent.click(getByRole("button", { name: /^Submit/i }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
  expect(body.tif).toBe("GTC");
});
```

Also add a combo-position version if feasible to prove combo and single-leg payload conventions remain aligned.

**Step 2: Run test to verify it fails**

Run:

```bash
cd web && npm test -- position-order-modal
```

Expected: FAIL because no GTC control exists and modal body does not explicitly include current local TIF.

**Step 3: Implement minimal modal change**

In `PositionOrderModal.tsx`, add state:

```ts
const [tif, setTif] = useState<"DAY" | "GTC">(draft.payload.tif ?? "DAY");
```

Include in submit body:

```ts
tif,
```

Render near outside RTH:

```tsx
<div className="modify-tif-toggle" role="group" aria-label="Time in force">
  {(["DAY", "GTC"] as const).map((value) => (
    <button
      key={value}
      type="button"
      className={tif === value ? "active" : ""}
      aria-pressed={tif === value}
      onClick={() => {
        setTif(value);
        attemptId.onFieldEdit("tif");
      }}
    >
      {value}
    </button>
  ))}
</div>
```

Reuse existing button classes where possible; do not add decorative gradients or raw hex.

**Step 4: Run test to verify it passes**

Run:

```bash
cd web && npm test -- position-order-modal
```

Expected: PASS.

**Step 5: Commit**

```bash
git add web/components/PositionOrderModal.tsx web/tests/position-order-modal.test.tsx
git commit -m "feat: add GTC control to position order modal"
```

---

### Task 6: Targeted Route And Browser Verification

**Files:**
- Add or modify targeted Playwright spec under `web/e2e/` if existing specs do not cover this flow.
- Candidate existing specs: `web/e2e/position-order-button.spec.ts`, order flow specs under `web/e2e/`.

**Step 1: Add or update browser regression**

Required browser assertions:

- Single-leg quote refresh calls `/api/orders/quote?...con_id=<real-id>`.
- Single-leg submit body includes `con_id: <real-id>`.
- A stale/backend quote failure displays `Quote unavailable: ...` or equivalent distinct copy and does not call `/api/orders/place`.
- Position modal DAY/GTC control can select GTC and request payload contains `tif: "GTC"`.

Prefer routing mocks:

```ts
await page.route("**/api/orders/quote**", async (route) => {
  quoteUrl = route.request().url();
  await route.fulfill({ status: 200, body: JSON.stringify({ token: "t.sig", bid: "1.20", ask: "1.30" }) });
});

await page.route("**/api/orders/place", async (route) => {
  placedBody = JSON.parse(route.request().postData() ?? "{}");
  await route.fulfill({ status: 200, body: JSON.stringify({ status: "ok", orderId: "abc" }) });
});
```

**Step 2: Run full targeted tests**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest scripts/tests/test_quote_route.py scripts/tests/test_place_quote_gate.py -xvs
cd web && npm test -- quote-token-client order-payload order-tab-reason-toast position-order-modal position-order-seed-ticket
cd web && npx playwright test web/e2e/<targeted-spec>.spec.ts
```

Expected: all targeted tests pass.

**Step 3: Run typecheck if dependency setup permits**

Run:

```bash
cd web && npm run typecheck
```

Expected: PASS.

**Step 4: Commit browser verification**

```bash
git add web/e2e/<targeted-spec>.spec.ts
git commit -m "test: cover order quote conId and modal GTC flows"
```

---

## Final Verification Checklist

Run:

```bash
git status --short
UV_CACHE_DIR=/tmp/uv-cache uv run pytest scripts/tests/test_quote_route.py scripts/tests/test_place_quote_gate.py -xvs
cd web && npm test -- quote-token-client order-payload order-tab-reason-toast position-order-modal position-order-seed-ticket
cd web && npx playwright test web/e2e/<targeted-spec>.spec.ts
```

Acceptance criteria:

- `/orders/quote` no longer calls IB from a raw worker thread without an event loop.
- Data role access for quote qualification/snapshot is serialized through `IBPool.acquire("data")`.
- Single-leg option quote request uses the position leg's real `conId`, never `0`.
- Single-leg option place body includes the same `con_id` used for quote token validation.
- Quote-fetch failures render distinct UI copy and do not silently degrade to `STALE_QUOTE`.
- Position order modal displays DAY/GTC, selecting GTC submits `tif: "GTC"`.
- Targeted Python, Vitest, and browser tests pass.

## Known Environment Blocker

Initial setup in this sandbox failed because network access is restricted:

- `uv sync --extra test --frozen` could not fetch PyPI packages.
- `npm ci` could not fetch `registry.npmjs.org`.

If local dependency caches are not already populated, run setup in a network-enabled environment before executing this plan.
