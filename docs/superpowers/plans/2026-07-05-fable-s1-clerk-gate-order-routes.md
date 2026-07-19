# S1 — Clerk-gate the order-mutating Next.js routes (SEC-1)

- **Date:** 2026-07-05
- **Branch:** `fix/s1-clerk-gate-order-routes`
- **Finding:** SEC-1 (fable deep review, reviewed at `4d864294`)
- **Severity:** High (Critical if the web port is ever exposed beyond the tailnet)
- **Goal (one line):** Every order/trade-mutating Next.js API route requires a Clerk
  session (or the existing dev/E2E bypass) before it proxies to FastAPI, so an
  unauthenticated caller with network reach to the web port can no longer place, cancel,
  modify, or wizard-submit orders.

---

## 1. Context (verified against HEAD)

- `web/middleware.ts` (line 12) marks **all** `/api/(.*)` public:
  `export const PUBLIC_ROUTES = ["/sign-in(.*)", "/sign-up(.*)", "/api/(.*)"];`
  This is **intentional and stays** — the file comment (lines 8–11) explains that
  server-side page fetches carry no Clerk cookie, so page reachability is gated at the
  middleware while **data access is meant to be gated inside the route handlers / FastAPI**.
  Order routes were never given that inner gate → the hole.
- `web/lib/xenonApi.ts::internalApiHeaders` (lines 27–33) attaches `X-Internal-Token` on
  every `xenonFetch` call **whenever `XENON_INTERNAL_API_TOKEN` is configured** (it is, in
  every real deployment), so FastAPI trusts the Next proxy as a transport peer. FastAPI's own Clerk JWT check is bypassed by that internal token → the
  Next layer is the **only** place a per-user gate can live for these proxied writes.
- **The exact pattern to copy already exists and is tested:**
  `web/app/api/admin/uw-quota/route.ts::GET` (lines 44–52) does an inline
  `auth()` + `userId` check with a `XENON_DISABLE_AUTH`/`PLAYWRIGHT_DISABLE_AUTH` bypass,
  and `web/tests/uw-quota-route.test.ts` mocks `@clerk/nextjs/server` to drive it. This
  plan factors that inline snippet into one shared helper and applies it to the mutating
  order/wizard/journal routes.
- **Clerk availability in route handlers (verified):** `@clerk/nextjs@^7.0.7`,
  `next@^16.1.6`. `auth()` is imported from `@clerk/nextjs/server` and awaited
  (`const { userId } = await auth();`). Because `clerkMiddleware` runs on `/api/(.*)`
  (matcher in `web/middleware.ts` line 32 includes `/(api|trpc)(.*)`), `auth()` inside an
  API route **can** read the browser's session cookie even though `isPublicRoute` lets the
  request through without `auth.protect()`. This is exactly how uw-quota works today.
- **Gated routes are browser-initiated** (verified by grepping callers): 9 of the 10 are
  fetched from `use client` components/hooks (`OrderTab.tsx`, `OptionsChainTab.tsx`,
  `BookTab.tsx`, `OrderActionsContext.tsx`, `useJournal.ts`, etc.), so the request carries
  the Clerk session cookie. Exception: **no browser caller was found for
  `wizard/sessions/[id]/protect`** (apparently a dormant/incomplete surface) — it is gated
  anyway, which can only tighten it. **None** of the gated routes is called during SSR / a
  server component fetch (those would carry no cookie and break). GET siblings — e.g.
  `wizard/sessions` GET, `journal` GET — are left public and untouched.

### What the executor does NOT need to understand

- IB order semantics, combo/BAG leg actions, naked-short logic, P&L math. This change adds
  an auth check **before** any body parsing or proxying; it never touches order payloads.
- FastAPI internals. The internal token stays as-is (transport trust only).

---

## 2. Drift from review

- **No code sketch exists** for SEC-1 in `docs/fable/11-code-sketches.md` (confirmed — the
  sketch sections §1–§8 cover other findings). This plan supplies the full implementation.
- Fable cites `web/lib/xenonApi.ts:27-44` for `internalApiHeaders`; at HEAD the function
  body is lines 27–33 (the `xenonFetch` token handling is lines 35–64). Mechanism unchanged;
  the citation line range drifted only. No behavioral drift.
- Finding still valid: order routes (`place`/`cancel`/`modify`) have **no** auth check at
  HEAD (verified by reading all three files).

---

## 3. Goal / Non-goals

### Goal

Add a single shared guard `requireOrderAuth()` and call it as the first statement of the
POST handler of every order/trade-mutating Next.js route. Unauthenticated → HTTP 401
`{ "error": "Unauthorized" }`. Dev/E2E bypass via the **same** env the middleware already
honors (`XENON_DISABLE_AUTH=1` or legacy `PLAYWRIGHT_DISABLE_AUTH=1`).

### Gated routes (exact, final list — 10 handlers)

| #   | File                                                | Method                  |
| --- | --------------------------------------------------- | ----------------------- |
| 1   | `web/app/api/orders/place/route.ts`                 | POST                    |
| 2   | `web/app/api/orders/cancel/route.ts`                | POST                    |
| 3   | `web/app/api/orders/modify/route.ts`                | POST                    |
| 4   | `web/app/api/wizard/plan/route.ts`                  | POST                    |
| 5   | `web/app/api/wizard/sessions/route.ts`              | POST (GET stays public) |
| 6   | `web/app/api/wizard/sessions/[id]/submit/route.ts`  | POST                    |
| 7   | `web/app/api/wizard/sessions/[id]/abort/route.ts`   | POST                    |
| 8   | `web/app/api/wizard/sessions/[id]/protect/route.ts` | POST                    |
| 9   | `web/app/api/wizard/sessions/[id]/reprice/route.ts` | POST                    |
| 10  | `web/app/api/journal/sync/route.ts`                 | POST                    |

### Non-goals (explicitly NOT changed here — keep one change / one PR)

- **Do NOT** narrow the middleware `PUBLIC_ROUTES` matcher. `/api/(.*)` stays public by
  design (server page fetches). `web/tests/middleware-route-gating.test.ts` pins this and
  must stay green.
- **Do NOT** gate non-order POST routes that merely trigger a read/refresh or are SSR-safe:
  `orders/route.ts` (refresh), `portfolio`, `futu/portfolio`, `blotter`, `attribution`,
  `performance`, `options/*`, `watchlist`. They are out of the SEC-1 scope (order mutation)
  and some are SSR-fetched — gating them risks the exact cookie-less-fetch breakage the
  middleware comment warns about. If a follow-up wants them, that is a separate PR.
- **Do NOT** touch `web/app/api/ib/ws-ticket/route.ts` — it is a thin proxy whose security
  model lives FastAPI-side (30 s single-use tickets, pop-on-validate); gating it is a
  separate decision, out of scope here.
- **Scope definition (tightened):** SEC-1 covers **order-execution mutations** — place,
  cancel, modify, wizard order lifecycle, journal sync. The sync/import POSTs
  (`orders/route.ts` refresh, `blotter`, `portfolio`, `futu/portfolio`) also write state and
  are denied to read-only query keys FastAPI-side, but they cannot create or change a broker
  order; gating them is a reasonable follow-up PR, deliberately NOT this one.
- **Do NOT** change `internalApiHeaders` / the FastAPI internal-token contract.
- **Do NOT** attempt OP-1/S2 (UNCERTAIN state), or any other finding.

---

## 4. Key facts (verified)

- Clerk import (route-handler side): `import { auth } from "@clerk/nextjs/server";`
  then `const { userId } = await auth();`. Verified in `web/app/api/admin/uw-quota/route.ts`.
- Bypass envs (both honored, OR-ed): `XENON_DISABLE_AUTH` and `PLAYWRIGHT_DISABLE_AUTH`,
  string value `"1"` means bypass. Verified in `web/middleware.ts` (lines 19–21) and
  `web/app/api/admin/uw-quota/route.ts` (lines 44–46).
- Playwright's dev server is started with `XENON_DISABLE_AUTH=1` (verified
  `web/playwright.config.ts` line 26 `webServer.command`), and `dev.sh --no-auth` exports
  `XENON_DISABLE_AUTH=1` (verified `scripts/infra/dev.sh` lines 32, 239) → all E2E and
  `--no-auth` dev flows bypass the gate automatically.
- Vitest config: `vitest.config.ts` at repo root; web test script is
  `NODE_ENV=test ASSISTANT_MOCK=1 vitest run --config ../vitest.config.ts web/tests`.
  **The config sets NO auth bypass env today.** Existing order/wizard route tests
  (`orders-place-idempotency-passthrough.test.ts`, `wizard-routes.test.ts`,
  `order-place-route-error-propagation.test.ts`, and order/wizard cases inside
  `api-routes-extended.test.ts`, `order-e2e.test.ts`, etc.) call the POST handlers directly
  **without** mocking Clerk. Adding the gate with no test-wide bypass would make `auth()`
  throw / return no `userId` and break them. **Step 1 sets the bypass test-wide** so all
  existing tests stay green; the new negative tests stub it OFF per-test to exercise the 401.
- The only existing test that references the bypass envs / asserts the 401 path on a gated
  helper is `web/tests/uw-quota-route.test.ts` (verified by grep). Its "returns 401 when
  there is no Clerk session" case (lines 48–54) will break once the bypass is test-wide, so
  Step 6 patches that one test to stub the bypass OFF. No other test file references these
  envs.
- Mutating route POST handler openings (exact anchors) — all confirmed single-POST-per-file
  except `wizard/sessions` (GET+POST; the `request: Request` signature makes the POST anchor
  unique):
  - `orders/place`, `orders/cancel`, `orders/modify`, `wizard/plan`, `wizard/sessions`:
    body begins `const requestId = getRequestId();`.
  - `wizard/sessions/[id]/{submit,abort,protect,reprice}`: signature is
    `POST(request: Request, context: { params: Promise<{ id: string }> })`, body begins
    `const requestId = getRequestId();`.
  - `journal/sync`: signature `POST(): Promise<Response>`, body begins `try {` (no
    `requestId`).

---

## 5. Steps (strictly ordered — TDD: failing test first where a test can be written before the code)

> Repo invariants to respect: web tests via `cd web && npm test`; typecheck via
> `cd web && npx tsc --noEmit`; never `git push origin master`; no AI-attribution commit
> trailers. This change touches **only web/** plus one docs row (incident history, §9); no Python code, no migrations, no FastAPI.

### Step 1 — Make the auth bypass test-wide (prevents mass test breakage)

**File:** `vitest.config.ts` (repo root).

**Anchor** — the `test: {` block, specifically the existing `environment: "node",` line:

```ts
    globalSetup: ["web/tests/setup/seed-fixtures.ts"],
    environment: "node",
```

**Change to** (add an `env` key immediately after `environment`):

```ts
    globalSetup: ["web/tests/setup/seed-fixtures.ts"],
    environment: "node",
    // SEC-1 (S1): order/wizard/journal mutation routes now call requireOrderAuth().
    // Default the dev/E2E auth bypass ON for the whole web test suite so existing
    // route-handler tests (which call POST directly without a Clerk session) stay
    // green. Tests that assert the 401 path stub this OFF per-test with
    // vi.stubEnv("XENON_DISABLE_AUTH", "").
    env: {
      XENON_DISABLE_AUTH: "1",
    },
```

Rationale (one line): a single test-wide env keeps ~15 existing order/wizard test files
untouched; the alternative (editing each) is far more error-prone.

> **After this step, run** `cd web && npm test -- uw-quota-route` — expect the
> "returns 401 when there is no Clerk session" case to **FAIL** (bypass now on). That
> failure is expected and is fixed in Step 6. Do not proceed past Step 6 with it red.

### Step 2 — Create the shared guard helper

**New file:** `web/lib/requireOrderAuth.ts`

```ts
import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

/**
 * Auth gate for order/trade-mutating Next.js API routes (SEC-1 / S1).
 *
 * `/api/(.*)` is public at the Next middleware because server-side page fetches
 * carry no Clerk cookie (see web/middleware.ts). Order, wizard, and journal
 * mutations are always browser-initiated, so the session cookie IS present — the
 * per-user gate lives here, in the route handler.
 *
 * Call as the first statement of every mutating POST handler:
 *
 *   export async function POST(request: Request): Promise<Response> {
 *     const unauthorized = await requireOrderAuth();
 *     if (unauthorized) return unauthorized;
 *     // ... existing handler body ...
 *   }
 *
 * Returns a 401 `{ error: "Unauthorized" }` Response when there is no Clerk
 * session. Returns `null` (proceed) when authenticated OR when the dev/E2E bypass
 * env is set — the SAME flags the middleware and dev.sh --no-auth honor
 * (XENON_DISABLE_AUTH=1 or legacy PLAYWRIGHT_DISABLE_AUTH=1). Env is read at call
 * time so per-request/test overrides take effect.
 */
export async function requireOrderAuth(): Promise<Response | null> {
  const authBypass =
    process.env.XENON_DISABLE_AUTH === "1" ||
    process.env.PLAYWRIGHT_DISABLE_AUTH === "1";
  if (authBypass) return null;

  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  return null;
}
```

### Step 3 — Write the failing unit test for the helper (RED)

**New file:** `web/tests/require-order-auth.test.ts`

```ts
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";

// Clerk's auth() is mocked so the gate can be driven per-test. vi.mock is
// hoisted, so the spy is created via vi.hoisted to be referenceable in the factory.
const { authMock } = vi.hoisted(() => ({ authMock: vi.fn() }));
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

import { requireOrderAuth } from "@/lib/requireOrderAuth";

beforeEach(() => {
  authMock.mockResolvedValue({ userId: "user_test" });
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

describe("requireOrderAuth", () => {
  it("returns 401 Unauthorized when there is no Clerk session and no bypass", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: null });

    const res = await requireOrderAuth();
    expect(res).not.toBeNull();
    expect(res!.status).toBe(401);
    const body = await res!.json();
    expect(body.error).toBe("Unauthorized");
  });

  it("returns null (proceed) when a Clerk session is present", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: "user_abc" });

    const res = await requireOrderAuth();
    expect(res).toBeNull();
  });

  it("bypasses auth (returns null) when XENON_DISABLE_AUTH=1 even with no session", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "1");
    authMock.mockResolvedValue({ userId: null });

    const res = await requireOrderAuth();
    expect(res).toBeNull();
  });

  it("bypasses auth (returns null) when legacy PLAYWRIGHT_DISABLE_AUTH=1", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "1");
    authMock.mockResolvedValue({ userId: null });

    const res = await requireOrderAuth();
    expect(res).toBeNull();
  });
});
```

> Run `cd web && npm test -- require-order-auth` now. Steps 2+3 together should be GREEN
> (the helper already exists). If Step 3 is somehow run before Step 2 exists, the import
> fails — that is the expected RED. Do not skip writing the helper.

### Step 4 — Add the guard to each order route

For **`web/app/api/orders/place/route.ts`**, **`.../orders/cancel/route.ts`**,
**`.../orders/modify/route.ts`**:

1. Add the import near the other `@/lib` imports at the top of the file:

```ts
import { requireOrderAuth } from "@/lib/requireOrderAuth";
```

2. Insert the guard as the first two statements of `POST`. Anchor (identical in all three
   files):

```ts
export async function POST(request: Request): Promise<Response> {
  const requestId = getRequestId();
```

Change to:

```ts
export async function POST(request: Request): Promise<Response> {
  const unauthorized = await requireOrderAuth();
  if (unauthorized) return unauthorized;
  const requestId = getRequestId();
```

### Step 5 — Add the guard to each wizard route + journal/sync

For **`wizard/plan`** and **`wizard/sessions`** (the POST handler only):

1. Add import:

```ts
import { requireOrderAuth } from "@/lib/requireOrderAuth";
```

2. Anchor (the POST handler — for `wizard/sessions` the `request: Request` signature makes
   it unique vs its `GET()`):

```ts
export async function POST(request: Request): Promise<Response> {
  const requestId = getRequestId();
```

Change to:

```ts
export async function POST(request: Request): Promise<Response> {
  const unauthorized = await requireOrderAuth();
  if (unauthorized) return unauthorized;
  const requestId = getRequestId();
```

For **`wizard/sessions/[id]/submit`**, **`.../abort`**, **`.../protect`**,
**`.../reprice`** (identical structure in all four):

1. Add import (same line as above).

2. Anchor:

```ts
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const requestId = getRequestId();
```

Change to:

```ts
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const unauthorized = await requireOrderAuth();
  if (unauthorized) return unauthorized;
  const requestId = getRequestId();
```

For **`web/app/api/journal/sync/route.ts`** (no `requestId` in this handler):

1. Add import:

```ts
import { requireOrderAuth } from "@/lib/requireOrderAuth";
```

2. Anchor:

```ts
export async function POST(): Promise<Response> {
  try {
```

Change to:

```ts
export async function POST(): Promise<Response> {
  const unauthorized = await requireOrderAuth();
  if (unauthorized) return unauthorized;
  try {
```

### Step 6 — Fix the one existing test the test-wide bypass breaks

**File:** `web/tests/uw-quota-route.test.ts`. The "returns 401 when there is no Clerk
session" case now needs to stub the bypass OFF (Step 1 turned it on suite-wide).

**Anchor:**

```ts
  it("returns 401 when there is no Clerk session", async () => {
    authMock.mockResolvedValue({ userId: null });
    const res = await GET();
```

**Change to:**

```ts
  it("returns 401 when there is no Clerk session", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: null });
    const res = await GET();
```

(The file's existing `afterEach` already calls `vi.unstubAllEnvs()`, so the stub is scoped
to this test.)

### Step 7 — Add route-level negative/positive integration tests

**New file:** `web/tests/order-route-auth-gate.test.ts` — proves the gate is actually wired
into **ALL 10** handlers, not just place. This matters because Step 1's suite-wide bypass
means the existing route tests pass whether or not a route was gated — a missed route would
be invisible without this test. Mocks `@/lib/xenonApi` so no FastAPI is needed and the
"allowed" path can't accidentally place a real order.

Structure: the three detailed cases below for `orders/place` (401 / session / bypass), plus a
**parameterized sweep** over all 10 handlers asserting the 401 direction for each:

```ts
// Parameterized 401 sweep — every gated handler must short-circuit before proxying.
// The [id] wizard routes take (request, context); journal/sync takes no args.
const GATED: Array<{ name: string; call: () => Promise<Response> }> = [
  {
    name: "orders/place",
    call: async () =>
      (await import("@/app/api/orders/place/route")).POST(req(validBody)),
  },
  {
    name: "orders/cancel",
    call: async () =>
      (await import("@/app/api/orders/cancel/route")).POST(req({ orderId: 1 })),
  },
  {
    name: "orders/modify",
    call: async () =>
      (await import("@/app/api/orders/modify/route")).POST(req({ orderId: 1 })),
  },
  {
    name: "wizard/plan",
    call: async () =>
      (await import("@/app/api/wizard/plan/route")).POST(req({})),
  },
  {
    name: "wizard/sessions",
    call: async () =>
      (await import("@/app/api/wizard/sessions/route")).POST(req({})),
  },
  ...["submit", "abort", "protect", "reprice"].map((leaf) => ({
    name: `wizard/sessions/[id]/${leaf}`,
    call: async () => {
      const mod = await import(`@/app/api/wizard/sessions/[id]/${leaf}/route`);
      return mod.POST(req({}), { params: Promise.resolve({ id: "sess-1" }) });
    },
  })),
  {
    name: "journal/sync",
    call: async () => (await import("@/app/api/journal/sync/route")).POST(),
  },
];

describe.each(GATED)("auth gate wired: $name", ({ call }) => {
  it("401s with no session and bypass off, without touching FastAPI", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: null });
    const res = await call();
    expect(res.status).toBe(401);
    expect(xenonFetchMock).not.toHaveBeenCalled();
  });
});
```

> If any single route file fails to import under the two mocks (it transitively needs another
> module the mock doesn't provide), mock that module minimally in this test file — do NOT drop
> the route from the sweep. The sweep's exit criterion is: all 10 names listed, all 10 401.

```ts
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";

const { authMock } = vi.hoisted(() => ({ authMock: vi.fn() }));
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

// Stub the FastAPI client so the "allowed" branch never proxies a real order.
const { xenonFetchMock } = vi.hoisted(() => ({ xenonFetchMock: vi.fn() }));
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: xenonFetchMock,
  XenonApiError: class XenonApiError extends Error {},
}));

import { POST } from "@/app/api/orders/place/route";

const validBody = {
  type: "stock",
  symbol: "TSLA",
  action: "BUY",
  quantity: 1,
  limitPrice: 393.45, // real TSLA close, 2026-07-02 (frozen fixture)
  client_attempt_id: "test-attempt-1",
};

function req(body: unknown): Request {
  return new Request("http://localhost/api/orders/place", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  authMock.mockResolvedValue({ userId: "user_test" });
  xenonFetchMock.mockResolvedValue({
    orderId: 123,
    permId: 456,
    initialStatus: "PreSubmitted",
    message: "ok",
  });
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

describe("POST /api/orders/place auth gate (SEC-1)", () => {
  it("returns 401 Unauthorized when no Clerk session and bypass off", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: null });

    const res = await POST(req(validBody));
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body.error).toBe("Unauthorized");
    // The gate must short-circuit BEFORE any FastAPI proxy call.
    expect(xenonFetchMock).not.toHaveBeenCalled();
  });

  it("proceeds past the gate when a Clerk session is present", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "");
    vi.stubEnv("PLAYWRIGHT_DISABLE_AUTH", "");
    authMock.mockResolvedValue({ userId: "user_abc" });

    const res = await POST(req(validBody));
    expect(res.status).not.toBe(401);
    expect(xenonFetchMock).toHaveBeenCalled();
  });

  it("proceeds past the gate under XENON_DISABLE_AUTH=1 with no session (dev/E2E)", async () => {
    vi.stubEnv("XENON_DISABLE_AUTH", "1");
    authMock.mockResolvedValue({ userId: null });

    const res = await POST(req(validBody));
    expect(res.status).not.toBe(401);
    expect(xenonFetchMock).toHaveBeenCalled();
  });
});
```

> **Frozen-price note:** `limitPrice: 393.45` is TSLA's real close on 2026-07-02 (matches the
> value referenced in the frozen-line backfill memory). If the executor cannot confirm a real
> frozen price, keep this value — it is a hardcoded fixture, never network-fetched, and the
> test asserts only status codes, not the price.

---

## 6. Verification matrix

Run every command below; each must produce the exact stated outcome.

### Unit (web) — helper + route gate

```bash
cd web && npm test -- require-order-auth
```

Expected: all 4 tests in `require-order-auth.test.ts` PASS (exit 0).

```bash
cd web && npm test -- order-route-auth-gate
```

Expected: all 13 tests PASS (3 detailed place cases + the 10-route parameterized 401 sweep). Critically, the 401 case asserts `xenonFetch` was **not**
called (proof the gate precedes the proxy).

```bash
cd web && npm test -- uw-quota-route
```

Expected: all cases PASS, including "returns 401 when there is no Clerk session" (now stubs
bypass off).

### Unit (web) — regression: existing order/wizard/journal route tests stay green

```bash
cd web && npm test -- wizard-routes orders-place-idempotency-passthrough order-place-route-error-propagation orders-cancel-pg orders-modify-pg orders-route-pg journal-sync-route-pg journal-sync
```

Expected: all PASS (Step 1's test-wide bypass keeps them unchanged).

### Full web suite

```bash
cd web && npm test
```

Expected: exit 0, no failures. (Pins `middleware-route-gating.test.ts`,
`auth-integration.test.ts`, and every order/wizard test still green.)

### Typecheck + lint (web touched)

```bash
cd web && npx tsc --noEmit
```

Expected: exit 0, no errors (new `requireOrderAuth.ts` types clean; `Response | null`).

```bash
cd web && npm run lint
```

Expected: exit 0.

### CI order-path guards (order routes touched — must stay green)

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
uv run python scripts/checks/no_json_write_on_order_path.py
uv run python scripts/checks/order_path_caller_allowlist.py
```

Expected: each exits 0. (This change adds no `readDataFile`/`json.load`/`writeFile`/
`_atomic_save` and no `ib_place_order` import — the guards should be unaffected. If any
guard fails, STOP — see tripwires.)

### Negative + positive directions (both proven)

- **Blocked without credential:** `order-route-auth-gate.test.ts` case 1 → 401, no proxy.
- **Allowed with credential:** case 2 → not 401, proxy called.
- **Dev bypass intact:** case 3 (`XENON_DISABLE_AUTH=1`) → not 401, proxy called;
  `require-order-auth.test.ts` also covers legacy `PLAYWRIGHT_DISABLE_AUTH=1`.

### E2E smoke (MANDATORY — order flow unaffected under bypass)

Playwright's dev server already runs with `XENON_DISABLE_AUTH=1` (`web/playwright.config.ts`
line 26), so the gate is bypassed for E2E and existing specs that exercise the order path
must still pass. Run one representative order-path spec:

```bash
cd web && npx playwright test e2e/open-order-combo.spec.ts
```

Expected: PASS. (If Playwright browsers aren't installed in this environment, this may skip
— note that in the report; the Vitest route-gate test above is the load-bearing proof.)

Optional manual chrome-cdp smoke against a paper dev stack (see live probes) — save
screenshot to `output/playwright/s1-order-gate-2026-07-05.png` showing an order placed
successfully while authenticated.

### Live probe (PAPER only — order path is paper-first per repo policy)

Only if a live smoke is wanted. Start the paper stack **without** `--no-auth` so the gate is
active, then confirm an unauthenticated curl is rejected:

```bash
scripts/infra/dev.sh paper        # Next :3200, FastAPI :8421 — do NOT pass --no-auth
# From another shell, unauthenticated (no Clerk cookie):
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:3200/api/orders/cancel \
  -H "Content-Type: application/json" -d '{"orderId":1}'
```

Expected: `401`. (With `--no-auth`, i.e. `XENON_DISABLE_AUTH=1`, the same curl would return
the normal handler response — that is the bypass working, not a regression.)

> Do NOT run any live-IB order placement. Paper only (IB port 4002). This change is auth-only
> and needs no real fill to verify.

---

## 7. Tripwires / abort criteria — STOP and report if:

1. **Any anchor snippet in Steps 4/5 is not found verbatim** in the target file (line
   numbers may have drifted since 2026-07-05, but the function signatures / `const requestId
= getRequestId();` openings are the anchors). Do not guess a new insertion point — stop
   and report the mismatch.
2. **`require-order-auth.test.ts` case "returns null when a Clerk session is present"
   FAILS** (i.e. the gate 401s an authenticated user) — the helper or its env logic is
   wrong; stop.
3. **A gated route test that was green before Step 1 goes red and is NOT
   `uw-quota-route`** — that means a route you gated is fetched somewhere without a session
   (possible SSR call you missed). Stop; do not blanket-add bypasses to hide it.
4. **More than the 16 files enumerated need edits** (1 vitest.config + 1 helper + 2 new
   test files + 10 route files + 1 uw-quota test patch + the
   `docs/reference/order-path-incident-history.md` row append = 16 total). If the list
   grows, stop — the scope is drifting.
5. **Any CI order-path guard (`no_json_*`, `order_path_caller_allowlist`) fails** — this
   change should not trip them; a failure means an unexpected interaction. Stop and report.
6. **`npx tsc --noEmit` reports an error** referencing `requireOrderAuth` or `Response |
null` — fix the helper's types before proceeding; do not `// @ts-expect-error`.
7. **Any step requires a live-IB call** — it doesn't. If you think it does, you've
   misread the task; use PAPER (`dev.sh paper`, port 4002) and never live money.

---

## 8. Rollback

- Pure additive/guard change, no schema, no migration. To revert: discard the branch
  (`git checkout master && git branch -D fix/s1-clerk-gate-order-routes`) or revert the
  single squash commit. No data migration to undo.
- If a regression is discovered post-merge (a gated route silently 401s a legitimate
  browser flow), the fastest safe mitigation is to export `XENON_DISABLE_AUTH=1` on the web
  process (restores pre-change behavior everywhere) while the specific route is
  investigated — but this reopens SEC-1, so treat it as an incident, not a fix.

---

## 9. Incident-history row (order surface touched — append to `docs/reference/order-path-incident-history.md`)

This is an auth-gate change on the order surface rather than an order-execution bug, but it
touches `web/app/api/orders/*`, so append a row for traceability. The table columns are
**`# | Date / PR | Issue | Root cause | Solution | Prevention`** (verified from the header at
`docs/reference/order-path-incident-history.md` line 28). Use the next sequential `#` (rows
are numbered; find the current max and add 1) and fill the real PR number once opened:

```
| N | 2026-07-05 #<PR> | Order-mutating Next.js routes (orders/place, orders/cancel, orders/modify + wizard plan/sessions/submit/abort/protect/reprice + journal/sync) had no per-user auth check | `/api/(.*)` is public at the Next middleware (page-fetch cookie constraint) and `xenonFetch` self-authenticates to FastAPI with `X-Internal-Token`, so any caller with network reach to the web port could place/cancel/modify orders (SEC-1, High) | Shared `web/lib/requireOrderAuth.ts` guard (Clerk `auth()` + `XENON_DISABLE_AUTH`/`PLAYWRIGHT_DISABLE_AUTH` bypass) called as the first statement of every mutating POST handler; middleware matcher unchanged | `web/tests/require-order-auth.test.ts`, `web/tests/order-route-auth-gate.test.ts`; both directions asserted (401 without session, proceed with session/bypass) |
```
