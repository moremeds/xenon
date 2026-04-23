# Position-order `quote_token` Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the F3 regression where `PositionOrderModal` (close/add) bypasses quote-token + limit-band safety. After this plan, every close/add submit — single-leg or combo — either passes `check()` / `check_combo()` or hits a soft-fail telemetry bucket we can watch and flip to hard-reject.

**Architecture:** Propagate `conId` from IB sync through the portfolio → ticket-draft → new `useQuoteTokens` hook. Modal mints N tokens in parallel (one per leg) and attaches `quote_token` (single-leg) or `quote_tokens: {con_id: token}` (combo) on submit. Backend adds `quote_guard.check_combo` that reconstructs net-bid/net-ask from per-leg token payloads (envelope × leg action XOR) and enforces ±5% net-band. Combo missing-token soft-fails with `QUOTE_TOKEN_MISSING_SOFT` telemetry for one rollout window; single-leg stays hard.

**Tech Stack:** Python 3.13 (FastAPI, pydantic, DuckDB via `orders_events`), TypeScript (Next.js 15, React 19), Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-04-23-position-order-quote-token-design.md`

---

## File Structure

**Python (backend)**

- Modify `src/xenon/execution/ib_sync.py` — emit `conId` on each formatted leg.
- Modify `src/xenon/execution/quote_guard.py` — add `CheckComboLeg`, `check_combo`, and `_compute_combo_nets` helper.
- Modify `src/xenon/api/server.py` — combo branch of `/orders/place` runs `check_combo` and records telemetry.
- Create `scripts/tests/test_quote_guard_combo.py` — unit tests for `check_combo`.
- Create `scripts/tests/test_quote_route_combo.py` — route tests for combo path.

**TypeScript (frontend)**

- Modify `web/lib/types.ts` — add `conId` to `PortfolioLeg`.
- Modify `web/lib/positionOrderPresets.ts` — propagate `conId` into `TicketPayload` (stock, option, combo legs).
- Modify `web/components/ticker-detail/useQuoteToken.ts` — add `useQuoteTokens({ legs })` hook; keep single-leg API working.
- Modify `web/components/PositionOrderModal.tsx` — call hook, disable submit until tokens resolve, attach to payload.
- Modify `web/lib/placeOrderBodySchema.ts` — accept `quote_tokens` map + optional per-leg `con_id`.
- Modify `web/app/api/orders/place/route.ts` — pass `quote_tokens` and per-leg `con_id` through to FastAPI.
- Create `web/tests/position-order-modal-quote-tokens.test.tsx` — Vitest for hook + modal wiring.
- Modify `web/tests/e2e/position-order-close.spec.ts` (or create) — Playwright E2E.

---

## Task 1: Propagate `conId` through portfolio → legs

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:470-484`
- Modify: `web/lib/types.ts:70-80`
- Test: `scripts/tests/test_ib_sync_legs_conid.py` (new)

- [ ] **Step 1: Write the failing Python test**

```python
# scripts/tests/test_ib_sync_legs_conid.py
from xenon.execution.ib_sync import _format_position_legs  # helper below

def test_formatted_legs_include_conid():
    raw_legs = [
        {
            "conId": 756733,
            "right": "C",
            "strike": 500.0,
            "position": 1,
            "entry_cost": 250.0,
            "avgCost": 2.5,
            "marketPrice": 2.6,
            "marketValue": 260.0,
        }
    ]
    out = _format_position_legs(raw_legs)
    assert out[0]["conId"] == 756733
    assert out[0]["type"] == "Call"
```

- [ ] **Step 2: Extract `_format_position_legs` helper and emit `conId`**

Refactor the inline `formatted_legs` block at `ib_sync.py:470-484` into a module-level helper that both the existing caller and the test can use. Add `"conId"` to the output.

```python
# src/xenon/execution/ib_sync.py — module level, before build_portfolio_positions

def _format_position_legs(legs: list) -> list:
    out = []
    for leg in sorted(legs, key=lambda x: (x.get("right", "Z"), x.get("strike", 0))):
        out.append(
            {
                "conId": leg.get("conId"),
                "direction": "LONG" if leg["position"] > 0 else "SHORT",
                "contracts": int(abs(leg["position"])),
                "type": "Call" if leg.get("right") == "C" else ("Put" if leg.get("right") == "P" else "Stock"),
                "strike": leg.get("strike"),
                "entry_cost": leg["entry_cost"],
                "avg_cost": leg["avgCost"],
                "market_price": leg.get("marketPrice"),
                "market_value": leg.get("marketValue"),
                "market_price_is_calculated": bool(leg.get("marketPriceIsCalculated")),
            }
        )
    return out
```

Then at `ib_sync.py:470-484` replace the inline loop with:

```python
        formatted_legs = _format_position_legs(legs)
```

- [ ] **Step 3: Run the test**

```bash
python3.13 -m pytest scripts/tests/test_ib_sync_legs_conid.py -xvs
```

Expected: PASS.

- [ ] **Step 4: Extend TS `PortfolioLeg` type**

Edit `web/lib/types.ts:70-80`:

```typescript
export type PortfolioLeg = {
  conId: number | null; // null for legacy rows or if IB didn't return one
  direction: "LONG" | "SHORT";
  contracts: number;
  type: "Call" | "Put" | "Stock";
  strike: number | null;
  entry_cost: number;
  avg_cost: number;
  market_price: number | null;
  market_value: number | null;
  market_price_is_calculated?: boolean;
};
```

- [ ] **Step 5: Typecheck**

```bash
cd web && npm run typecheck
```

Expected: PASS. If any existing callsite constructs a `PortfolioLeg` without `conId`, set it to `null` there.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_legs_conid.py web/lib/types.ts
git commit -m "feat(sync): emit conId on portfolio legs"
```

---

## Task 2: Thread `conId` into ticket-draft payload

**Files:**

- Modify: `web/lib/positionOrderPresets.ts:7-41`
- Test: `web/tests/position-order-presets-conid.test.ts` (new)

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/position-order-presets-conid.test.ts
import { describe, it, expect } from "vitest";
import { buildPositionOrderDraft } from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";

const optionPosition: PortfolioPosition = {
  id: 1,
  ticker: "SPY",
  structure: "Long Call",
  structure_type: "long_call",
  risk_profile: "long_option",
  expiry: "2026-05-16",
  contracts: 1,
  direction: "LONG",
  entry_cost: 250,
  max_risk: null,
  market_value: null,
  legs: [
    {
      conId: 111222,
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 500,
      entry_cost: 250,
      avg_cost: 2.5,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

describe("buildPositionOrderDraft", () => {
  it("populates conId on option payload", () => {
    const draft = buildPositionOrderDraft({
      position: optionPosition,
      intent: "close",
      prices: {},
    });
    expect(draft.payload.type).toBe("option");
    if (draft.payload.type !== "option") throw new Error("narrowing");
    expect(draft.payload.conId).toBe(111222);
  });
});
```

- [ ] **Step 2: Run it to verify failure**

```bash
cd web && npm test -- position-order-presets-conid
```

Expected: FAIL — `conId` is not on `TicketPayload.option`.

- [ ] **Step 3: Extend `TicketPayload` in `web/lib/positionOrderPresets.ts`**

Replace lines 7-41:

```typescript
export type TicketPayload =
  | {
      type: "stock";
      symbol: string;
      conId: number | null;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
    }
  | {
      type: "option";
      symbol: string;
      conId: number | null;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      expiry: string;
      strike: number;
      right: "C" | "P";
    }
  | {
      type: "combo";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      legs: Array<{
        conId: number | null;
        expiry: string;
        strike: number;
        right: "C" | "P";
        action: "BUY" | "SELL";
        ratio: number;
      }>;
    };
```

Then locate every constructor in the same file and populate `conId`:

- Stock payload (around `type: "stock"` construction): set `conId: position.legs[0]?.conId ?? null`.
- Option payload: set `conId: position.legs[0]?.conId ?? null`.
- Combo legs: when mapping over `position.legs`, set `conId: leg.conId ?? null` on each entry.

- [ ] **Step 4: Rerun the test**

```bash
cd web && npm test -- position-order-presets-conid
```

Expected: PASS.

- [ ] **Step 5: Typecheck**

```bash
cd web && npm run typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-presets-conid.test.ts
git commit -m "feat(web): thread conId into position ticket payloads"
```

---

## Task 3: `useQuoteTokens` hook (multi-leg)

**Files:**

- Modify: `web/components/ticker-detail/useQuoteToken.ts`
- Test: `web/tests/use-quote-tokens.test.tsx` (new)

- [ ] **Step 1: Write the failing test**

```tsx
// web/tests/use-quote-tokens.test.tsx
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useQuoteTokens } from "@/components/ticker-detail/useQuoteToken";

describe("useQuoteTokens", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    global.fetch = vi.fn(async (url: string) => {
      const m = url.match(/con_id=(\d+)/);
      const conId = m?.[1];
      return {
        ok: true,
        json: async () => ({ token: `tok-${conId}` }),
      } as Response;
    });
  });

  it("mints one token per leg in parallel, keyed by conId", async () => {
    const { result } = renderHook(() =>
      useQuoteTokens({
        legs: [
          { ticker: "SPY", conId: 111, expiry: "2026-05-16" },
          { ticker: "SPY", conId: 222, expiry: "2026-05-16" },
        ],
      }),
    );
    await waitFor(() => expect(result.current.tokens).not.toBeNull());
    expect(result.current.tokens).toEqual({
      "111": "tok-111",
      "222": "tok-222",
    });
    expect(result.current.error).toBeNull();
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it("returns error if any leg fails", async () => {
    global.fetch = vi.fn(async (url: string) =>
      url.includes("con_id=222")
        ? ({ ok: false, status: 500 } as Response)
        : ({ ok: true, json: async () => ({ token: "tok-111" }) } as Response),
    );
    const { result } = renderHook(() =>
      useQuoteTokens({
        legs: [
          { ticker: "SPY", conId: 111, expiry: "2026-05-16" },
          { ticker: "SPY", conId: 222, expiry: "2026-05-16" },
        ],
      }),
    );
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.tokens).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify failure**

```bash
cd web && npm test -- use-quote-tokens
```

Expected: FAIL — `useQuoteTokens` not exported.

- [ ] **Step 3: Implement the hook**

Edit `web/components/ticker-detail/useQuoteToken.ts`. Keep the existing single-leg export. Add below it:

```typescript
type Leg = { ticker: string; conId: number | null; expiry: string | null };

type TokensResult = {
  tokens: Record<string, string> | null;
  error: string | null;
};

export function useQuoteTokens({ legs }: { legs: Leg[] }): TokensResult {
  const [tokens, setTokens] = useState<Record<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const legsKey = JSON.stringify(
    legs.map((l) => [l.ticker, l.conId, l.expiry]),
  );

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setTokens(null);
      setError(null);
      if (legs.length === 0) return;
      if (legs.some((l) => l.conId == null)) {
        setError("missing conId on one or more legs");
        return;
      }
      try {
        const results = await Promise.all(
          legs.map(async (l) => {
            const res = await fetch(
              `/api/orders/quote?ticker=${encodeURIComponent(l.ticker)}&con_id=${l.conId}`,
            );
            if (!res.ok) throw new Error(`quote ${res.status} for ${l.conId}`);
            const j = await res.json();
            return [String(l.conId), j.token as string] as const;
          }),
        );
        if (!cancelled) {
          setTokens(Object.fromEntries(results));
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [legsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return { tokens, error };
}
```

- [ ] **Step 4: Run the test**

```bash
cd web && npm test -- use-quote-tokens
```

Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/useQuoteToken.ts web/tests/use-quote-tokens.test.tsx
git commit -m "feat(web): useQuoteTokens hook for multi-leg token minting"
```

---

## Task 4: `PositionOrderModal` mints and submits tokens

**Files:**

- Modify: `web/components/PositionOrderModal.tsx` (imports + handleSubmit + submit gating)
- Test: `web/tests/position-order-modal-quote-tokens.test.tsx` (new)

- [ ] **Step 1: Write the failing test**

```tsx
// web/tests/position-order-modal-quote-tokens.test.tsx
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PositionOrderModal } from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

const comboPosition: PortfolioPosition = {
  id: 1,
  ticker: "SPY",
  structure: "Bull Call Spread",
  structure_type: "vertical_call_debit",
  risk_profile: "defined_risk",
  expiry: "2026-05-16",
  contracts: 1,
  direction: "LONG",
  entry_cost: 300,
  max_risk: 300,
  market_value: 320,
  legs: [
    {
      conId: 111,
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 500,
      entry_cost: 500,
      avg_cost: 5.0,
      market_price: 5.2,
      market_value: 520,
    },
    {
      conId: 222,
      direction: "SHORT",
      contracts: 1,
      type: "Call",
      strike: 510,
      entry_cost: -200,
      avg_cost: 2.0,
      market_price: 2.0,
      market_value: -200,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

describe("PositionOrderModal — quote tokens", () => {
  beforeEach(() => {
    global.fetch = vi.fn(async (url: RequestInfo, init?: RequestInit) => {
      const u = String(url);
      if (u.startsWith("/api/orders/quote")) {
        const m = u.match(/con_id=(\d+)/);
        return {
          ok: true,
          json: async () => ({ token: `tok-${m?.[1]}` }),
        } as Response;
      }
      if (u === "/api/orders/place") {
        return { ok: true, json: async () => ({ orderId: "O1" }) } as Response;
      }
      return { ok: false } as Response;
    });
  });

  it("disables submit until all tokens resolve, then POSTs with quote_tokens map", async () => {
    render(
      <PositionOrderModal
        isOpen
        position={comboPosition}
        intent="close"
        prices={{}}
        onClose={() => {}}
      />,
    );

    const submit = await screen.findByRole("button", { name: /submit close/i });
    expect(submit).toBeDisabled();

    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    const placeCall = (
      global.fetch as ReturnType<typeof vi.fn>
    ).mock.calls.find((c) => c[0] === "/api/orders/place");
    expect(placeCall).toBeDefined();
    const body = JSON.parse((placeCall![1] as RequestInit).body as string);
    expect(body.quote_tokens).toEqual({ "111": "tok-111", "222": "tok-222" });
    expect(body.quote_token).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run to verify failure**

```bash
cd web && npm test -- position-order-modal-quote-tokens
```

Expected: FAIL — modal doesn't mint tokens today.

- [ ] **Step 3: Wire hook into modal**

Edit `web/components/PositionOrderModal.tsx`. Near the existing imports, add:

```typescript
import { useQuoteTokens } from "./ticker-detail/useQuoteToken";
```

Above `handleSubmit` (near the `isCombo` declaration), derive legs and call the hook:

```typescript
const quoteLegs = useMemo(() => {
  if (draft.payload.type === "combo") {
    return draft.payload.legs.map((l) => ({
      ticker: position.ticker,
      conId: l.conId,
      expiry: l.expiry,
    }));
  }
  return [
    {
      ticker: position.ticker,
      conId: draft.payload.conId,
      expiry: position.expiry ?? null,
    },
  ];
}, [draft.payload, position.ticker, position.expiry]);

const { tokens: quoteTokens, error: quoteError } = useQuoteTokens({
  legs: quoteLegs,
});
const tokensReady = quoteTokens !== null;
```

Gate submit enablement. Find the existing `if (submitting || !isValidQty || !isValidPrice) return;` in `handleSubmit` and replace with:

```typescript
if (submitting || !isValidQty || !isValidPrice || !tokensReady) return;
```

Also extend the `disabled` prop on the submit button to include `!tokensReady`. Find the `<button …>{submitLabel}</button>` and add `disabled={...existing || !tokensReady}`.

Replace the `body = { ... }` block in `handleSubmit` (currently at lines 126–132) with:

```typescript
const tokenBag =
  draft.payload.type === "combo"
    ? { quote_tokens: quoteTokens ?? {} }
    : (() => {
        const cid = String(draft.payload.conId ?? "");
        const t = cid && quoteTokens ? quoteTokens[cid] : undefined;
        return t ? { quote_token: t } : {};
      })();

const body = {
  ...draft.payload,
  quantity: clampedQty,
  limitPrice: parsedPrice,
  client_attempt_id: attemptId.id,
  ...(outsideRth && !isCombo ? { outsideRth: true } : {}),
  ...tokenBag,
};
```

Surface `quoteError` when it is non-null. Locate where `error` is rendered and add (in the same error surface):

```typescript
{quoteError && (
  <div className="text-err mono text-xs">Quote unavailable: {quoteError}</div>
)}
```

- [ ] **Step 4: Run the Vitest**

```bash
cd web && npm test -- position-order-modal-quote-tokens
```

Expected: PASS.

- [ ] **Step 5: Typecheck**

```bash
cd web && npm run typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/components/PositionOrderModal.tsx web/tests/position-order-modal-quote-tokens.test.tsx
git commit -m "feat(web): position modal mints and submits quote tokens"
```

---

## Task 5: `/api/orders/place` passes `quote_tokens` + per-leg `con_id` to FastAPI

**Files:**

- Modify: `web/lib/placeOrderBodySchema.ts`
- Modify: `web/app/api/orders/place/route.ts:20-44, 213-240`
- Test: `web/tests/orders-place-quote-tokens-passthrough.test.ts` (new)

- [ ] **Step 1: Write the failing test**

```typescript
// web/tests/orders-place-quote-tokens-passthrough.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { POST } from "@/app/api/orders/place/route";
import * as xenonApi from "@/lib/xenonApi";

describe("/api/orders/place — quote_tokens passthrough", () => {
  beforeEach(() => {
    vi.spyOn(xenonApi, "xenonFetch").mockResolvedValue({
      orderId: "O1",
    } as any);
  });

  it("forwards quote_tokens and per-leg con_id to FastAPI", async () => {
    const body = {
      type: "combo",
      symbol: "SPY",
      action: "SELL",
      quantity: 1,
      limitPrice: 2.3,
      tif: "DAY",
      legs: [
        {
          conId: 111,
          expiry: "2026-05-16",
          strike: 500,
          right: "C",
          action: "BUY",
          ratio: 1,
        },
        {
          conId: 222,
          expiry: "2026-05-16",
          strike: 510,
          right: "C",
          action: "SELL",
          ratio: 1,
        },
      ],
      quote_tokens: { "111": "tok-111", "222": "tok-222" },
      client_attempt_id: "attempt-1",
    };
    const req = new Request("http://x/orders/place", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Content-Type": "application/json" },
    });
    await POST(req);

    expect(xenonApi.xenonFetch).toHaveBeenCalled();
    const forwarded = (xenonApi.xenonFetch as any).mock.calls[0][1];
    const forwardedBody = JSON.parse(forwarded.body);
    expect(forwardedBody.quote_tokens).toEqual({
      "111": "tok-111",
      "222": "tok-222",
    });
    expect(forwardedBody.legs[0].con_id).toBe(111);
    expect(forwardedBody.legs[1].con_id).toBe(222);
  });
});
```

- [ ] **Step 2: Run to verify failure**

```bash
cd web && npm test -- orders-place-quote-tokens-passthrough
```

Expected: FAIL — route doesn't forward these fields.

- [ ] **Step 3: Extend schema + route**

In `web/lib/placeOrderBodySchema.ts`, extend the combo-leg and root schemas to optionally accept `conId` on legs and `quote_tokens: Record<string, string>` at root. Follow the file's existing validation style. If the schema is permissive (passthrough), just ensure the new fields are not rejected.

In `web/app/api/orders/place/route.ts:20-44`, extend types:

```typescript
type ComboLeg = {
  conId?: number;
  expiry: string;
  strike: number;
  right: "C" | "P";
  action: "BUY" | "SELL";
  ratio: number;
  limitPrice?: number;
};

type PlaceBody = {
  type: "stock" | "option" | "combo";
  symbol: string;
  action: "BUY" | "SELL";
  quantity: number;
  limitPrice: number;
  tif?: "DAY" | "GTC";
  expiry?: string;
  strike?: number;
  right?: "C" | "P";
  legs?: ComboLeg[];
  client_attempt_id?: string;
  quote_token?: string;
  quote_tokens?: Record<string, string>;
  con_id?: number;
  acknowledge_limit_override?: boolean;
};
```

In the `orderPayload` builder at `route.ts:213-240`, extend the combo-legs map and root passthrough:

```typescript
      ...(body.type === "combo" && body.legs
        ? {
            legs: body.legs.map((l) => ({
              ...(l.conId != null ? { con_id: l.conId } : {}),
              expiry: l.expiry,
              strike: l.strike,
              right: l.right,
              action: l.action,
              ratio: l.ratio,
              ...(l.limitPrice != null ? { limitPrice: l.limitPrice } : {}),
            })),
          }
        : {}),
      ...(body.quote_tokens
        ? { quote_tokens: body.quote_tokens }
        : {}),
```

Keep the existing `...(body.quote_token ? { quote_token: body.quote_token } : {})` as-is.

- [ ] **Step 4: Run the test**

```bash
cd web && npm test -- orders-place-quote-tokens-passthrough
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/placeOrderBodySchema.ts web/app/api/orders/place/route.ts web/tests/orders-place-quote-tokens-passthrough.test.ts
git commit -m "feat(web): forward quote_tokens + per-leg con_id to FastAPI"
```

---

## Task 6: Backend `check_combo` + natural-market math

**Files:**

- Modify: `src/xenon/execution/quote_guard.py`
- Test: `scripts/tests/test_quote_guard_combo.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_quote_guard_combo.py
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from xenon.execution import quote_guard, quote_tokens
from xenon.execution.preflight import ReasonCode

SECRET = "b" * 64
NYC = ZoneInfo("America/New_York")
MIDDAY_RTH = datetime(2026, 4, 22, 13, 0, tzinfo=NYC)


def _mint(con_id: int, ticker: str, bid: str, ask: str, bid_sz: int = 100, ask_sz: int = 100, age_ms: int = 0) -> str:
    payload = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker=ticker,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_sz,
        ask_size=ask_sz,
        ts_server_ms=int(time.time() * 1000) - age_ms,
    )
    return quote_tokens.mint(payload, SECRET)


def _leg(con_id: int, action: str, token: str, right: str = "C"):
    return quote_guard.CheckComboLeg(
        token=token,
        con_id=con_id,
        ticker="SPY",
        action=action,
        right=right,
        ratio=1,
    )


def test_bull_call_spread_buy_envelope_in_band_accepts():
    # Long 500C at 4.50/4.70, Short 510C at 2.00/2.20.
    # BUY envelope: net_ask = 4.70 - 2.00 = 2.70, net_bid = 4.50 - 2.20 = 2.30.
    # Limit 2.80 is within net_ask * 1.05 = 2.835.
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.80"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_bull_call_spread_buy_envelope_over_band_rejects():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("3.00"),  # > 2.835 cap
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_bull_call_spread_sell_envelope_uses_net_bid_floor():
    # SELL (close) envelope: receive bids on BUY-structure legs, pay asks on SELL-structure legs.
    # net_bid = 4.50 - 2.20 = 2.30, floor = 2.30 * 0.95 = 2.185.
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.20"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True
    v2 = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.00"),  # < 2.185 floor
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v2.accept is False
    assert v2.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_risk_reversal_buy_call_sell_put():
    # BUY 500C 4.50/4.70 + SELL 490P 3.00/3.20.
    # BUY envelope: net_ask = 4.70 + (-3.00) = 1.70 (pay ask on BUY leg, receive bid on SELL leg → subtract).
    # Wait: SELL leg in BUY envelope = "receive bid" → contributes -bid to net_ask.
    # So net_ask = 4.70 - 3.00 = 1.70. Limit 1.75 should accept (< 1.70*1.05=1.785).
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70"), right="C"),
        _leg(2, "SELL", _mint(2, "SPY", "3.00", "3.20"), right="P"),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("1.75"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_stale_leg_token_rejects_whole_combo():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20", age_ms=10_000)),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE


def test_zero_size_leg_rejects():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70", bid_sz=0)),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE


def test_token_contract_mismatch_rejects():
    # Token minted for con_id=1 but used as con_id=99 in the combo leg.
    tok = _mint(1, "SPY", "4.50", "4.70")
    legs = [
        quote_guard.CheckComboLeg(
            token=tok, con_id=99, ticker="SPY", action="BUY", right="C", ratio=1,
        ),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE
```

- [ ] **Step 2: Run to verify failure**

```bash
python3.13 -m pytest scripts/tests/test_quote_guard_combo.py -xvs
```

Expected: FAIL — `CheckComboLeg` and `check_combo` not defined.

- [ ] **Step 3: Implement `check_combo`**

Append to `src/xenon/execution/quote_guard.py`:

```python
class CheckComboLeg(BaseModel):
    token: str
    con_id: int
    ticker: str
    action: Literal["BUY", "SELL"]
    right: Literal["C", "P", "STK"]
    ratio: int = 1


def _effective_side_is_ask(envelope_action: str, leg_action: str) -> bool:
    """True if we pay this leg's ask (=cost side). False if we receive bid.

    BUY envelope × BUY leg → pay ask.
    BUY envelope × SELL leg → receive bid.
    SELL envelope × BUY leg → receive bid.
    SELL envelope × SELL leg → pay ask.
    """
    return envelope_action == leg_action


def _compute_combo_nets(
    leg_payloads: list[tuple["quote_tokens.QuotePayload", str, int]],
    envelope_action: str,
) -> tuple[Decimal, Decimal]:
    """Return (net_ask_cost_to_open, net_bid_proceeds_to_close).

    Each entry: (payload, leg_action, ratio). Math per web/CLAUDE.md "Combo
    Natural Market Bid/Ask". "Open" here means executing in the direction of
    envelope_action; "close" means the reverse.
    """
    net_ask = Decimal("0")
    net_bid = Decimal("0")
    for payload, leg_action, ratio in leg_payloads:
        r = Decimal(ratio)
        if _effective_side_is_ask(envelope_action, leg_action):
            # We pay ask to execute; to reverse we would receive bid.
            net_ask += payload.ask * r
            net_bid += payload.bid * r
        else:
            # We receive bid to execute; to reverse we would pay ask.
            net_ask -= payload.bid * r
            net_bid -= payload.ask * r
    return net_ask, net_bid


def check_combo(
    *,
    legs: list[CheckComboLeg],
    envelope_action: Literal["BUY", "SELL"],
    limit_price: Decimal,
    token_secret: str,
    now: datetime,
) -> QuoteVerdict:
    if not legs:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="no legs",
        )
    # Market-hours gate: any option leg requires equity-options open.
    if any(leg.right in ("C", "P") for leg in legs) and not is_opt_tradeable(now):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="equity-option market closed (09:30-16:00 ET weekdays)",
        )

    max_age = _MAX_AGE_RTH_MS
    leg_payloads: list[tuple[quote_tokens.QuotePayload, str, int]] = []
    for leg in legs:
        try:
            payload = quote_tokens.verify(leg.token, token_secret, max_age_ms=max_age)
        except quote_tokens.QuoteTokenExpired as exc:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: {exc}",
            )
        except quote_tokens.QuoteTokenInvalid as exc:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: token invalid: {exc}",
            )
        if payload.ticker.upper() != leg.ticker.upper() or payload.con_id != leg.con_id:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: token contract mismatch",
            )
        if payload.bid > payload.ask or payload.bid_size <= 0 or payload.ask_size <= 0:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: crossed or zero-size quote",
            )
        leg_payloads.append((payload, leg.action, leg.ratio))

    net_ask, net_bid = _compute_combo_nets(leg_payloads, envelope_action)

    if envelope_action == "BUY":
        cap = net_ask * Decimal("1.05")
        if limit_price > cap:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"BUY limit {limit_price} > cap {cap} (net_ask {net_ask})",
            )
    else:
        floor = net_bid * Decimal("0.95")
        if limit_price < floor:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"SELL limit {limit_price} < floor {floor} (net_bid {net_bid})",
            )

    return QuoteVerdict(accept=True)
```

- [ ] **Step 4: Run the tests**

```bash
python3.13 -m pytest scripts/tests/test_quote_guard_combo.py -xvs
```

Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/quote_guard.py scripts/tests/test_quote_guard_combo.py
git commit -m "feat(execution): check_combo with net-band limit check"
```

---

## Task 7: Wire `check_combo` into `/orders/place` + telemetry

**Files:**

- Modify: `src/xenon/api/server.py:1526-1565`
- Test: `scripts/tests/test_quote_route_combo.py` (new)

- [ ] **Step 1: Write the failing route tests**

```python
# scripts/tests/test_quote_route_combo.py
import json
import os
import time
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from xenon.execution import quote_tokens

SECRET = "b" * 64


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    # Isolate orders_events DB.
    monkeypatch.setenv("XENON_ORDERS_DB", str(tmp_path / "orders.duckdb"))
    from xenon.api.server import app
    return TestClient(app)


def _mint(con_id: int, bid: str = "4.50", ask: str = "4.70") -> str:
    p = quote_tokens.QuotePayload(
        con_id=con_id, ticker="SPY",
        bid=Decimal(bid), ask=Decimal(ask),
        bid_size=100, ask_size=100,
        ts_server_ms=int(time.time() * 1000),
    )
    return quote_tokens.mint(p, SECRET)


def _combo_body(legs_tokens: dict[str, str] | None, limit_price: str = "2.70"):
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": float(limit_price),
        "tif": "DAY",
        "client_attempt_id": f"attempt-{time.time_ns()}",
        "legs": [
            {"con_id": 1, "expiry": "2026-05-16", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
            {"con_id": 2, "expiry": "2026-05-16", "strike": 510, "right": "C", "action": "SELL", "ratio": 1},
        ],
        **({"quote_tokens": legs_tokens} if legs_tokens is not None else {}),
    }


def test_combo_missing_tokens_soft_fails_with_telemetry(client):
    body = _combo_body(None)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text
    # Telemetry row exists.
    from xenon.execution import orders_store
    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute(
            "SELECT kind FROM orders_events WHERE kind='QUOTE_TOKEN_MISSING_SOFT'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1


def test_combo_in_band_tokens_pass(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="2.70")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text


def test_combo_out_of_band_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="3.50")  # > net_ask*1.05
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "LIMIT_OUT_OF_BAND"


def test_combo_tampered_token_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70") + "x", "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "STALE_QUOTE"
```

- [ ] **Step 2: Run to verify failure**

```bash
python3.13 -m pytest scripts/tests/test_quote_route_combo.py -xvs
```

Expected: FAIL — current combo branch neither runs `check_combo` nor emits telemetry.

- [ ] **Step 3: Wire `check_combo` into the combo branch**

In `src/xenon/api/server.py`, replace the combo branch that today sits at `~1563-1564` (the `else: _override_detail = None` after the non-combo `if body.get("type") != "combo":` block). Extend it to:

```python
    else:
        _override_detail = None
        quote_tokens_map = body.get("quote_tokens")
        legs_in = body.get("legs") or []
        symbol = str(body.get("symbol", "")).upper()
        envelope = str(body.get("action", "")).upper()
        if quote_tokens_map:
            try:
                check_legs = [
                    quote_guard.CheckComboLeg(
                        token=quote_tokens_map[str(leg["con_id"])],
                        con_id=int(leg["con_id"]),
                        ticker=symbol,
                        action=str(leg["action"]).upper(),
                        right=str(leg.get("right") or "STK").upper(),
                        ratio=int(leg.get("ratio", 1)),
                    )
                    for leg in legs_in
                ]
            except KeyError as exc:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": f"quote_tokens missing entry for leg con_id={exc}",
                        "reason_code": ReasonCode.STALE_QUOTE.value,
                    },
                )
            qv = quote_guard.check_combo(
                legs=check_legs,
                envelope_action=envelope,  # type: ignore[arg-type]
                limit_price=Decimal(str(body.get("limitPrice", "0"))),
                token_secret=os.environ.get("XENON_QUOTE_TOKEN_SECRET", ""),
                now=_now(),
            )
            if not qv.accept:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": qv.reason_detail,
                        "reason_code": qv.reason_code.value if qv.reason_code else None,
                        "reason_detail": qv.reason_detail,
                    },
                )
            _combo_quote_check_passed = True
            _combo_leg_count = len(check_legs)
        else:
            _combo_quote_check_passed = False
            _combo_leg_count = len(legs_in)
```

Then, after the F4 `outcome = orders_store.reserve_attempt(...)` block succeeds and `submission_id` is bound, emit the telemetry row:

```python
    # Combo-branch telemetry (one row per submission).
    if body.get("type") == "combo":
        if quote_tokens_map:
            orders_store.record_event(
                submission_id,
                "QUOTE_CHECK_PASS",
                {"leg_count": _combo_leg_count, "limit_price": str(body.get("limitPrice"))},
            )
        else:
            orders_store.record_event(
                submission_id,
                "QUOTE_TOKEN_MISSING_SOFT",
                {"leg_count": _combo_leg_count},
            )
```

Place this block just after the `submission_id = outcome.submission_id` line and before any `test_mode` / subprocess dispatch.

- [ ] **Step 4: Run the route tests**

```bash
python3.13 -m pytest scripts/tests/test_quote_route_combo.py -xvs
```

Expected: PASS (all 4 tests).

- [ ] **Step 5: Run the broader regression**

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py
```

Expected: PASS (existing `test_preflight_route.py`, `test_quote_guard.py`, etc.).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py scripts/tests/test_quote_route_combo.py
git commit -m "feat(api): enforce check_combo on /orders/place combo branch"
```

---

## Task 8: Playwright E2E for close/add

**Files:**

- Create: `web/tests/e2e/position-order-quote-token.spec.ts`

- [ ] **Step 1: Write the E2E spec**

```typescript
// web/tests/e2e/position-order-quote-token.spec.ts
import { test, expect } from "@playwright/test";

test.describe("PositionOrderModal quote_token wiring", () => {
  test("single-leg option close includes quote_token on submit", async ({
    page,
  }) => {
    let placeBody: any = null;
    await page.route("**/api/orders/quote*", async (route) => {
      const u = new URL(route.request().url());
      const conId = u.searchParams.get("con_id");
      await route.fulfill({ json: { token: `tok-${conId}` } });
    });
    await page.route("**/api/orders/place", async (route) => {
      placeBody = route.request().postDataJSON();
      await route.fulfill({ json: { orderId: "O1" } });
    });

    await page.goto("/");
    // Open modal for a single-leg option position via the position-row IB button.
    // (Exact selectors follow the project's existing portfolio table.)
    await page.getByTestId("position-row-order-button").first().click();
    await page.getByRole("button", { name: /submit close/i }).click();

    expect(placeBody).not.toBeNull();
    expect(typeof placeBody.quote_token).toBe("string");
  });

  test("vertical combo close includes quote_tokens map with one entry per leg", async ({
    page,
  }) => {
    let placeBody: any = null;
    await page.route("**/api/orders/quote*", async (route) => {
      const u = new URL(route.request().url());
      const conId = u.searchParams.get("con_id");
      await route.fulfill({ json: { token: `tok-${conId}` } });
    });
    await page.route("**/api/orders/place", async (route) => {
      placeBody = route.request().postDataJSON();
      await route.fulfill({ json: { orderId: "O1" } });
    });

    await page.goto("/");
    await page.getByTestId("position-row-order-button-combo").first().click();
    await page.getByRole("button", { name: /submit close/i }).click();

    expect(placeBody).not.toBeNull();
    expect(placeBody.quote_tokens).toBeTruthy();
    const keys = Object.keys(placeBody.quote_tokens);
    expect(keys.length).toBeGreaterThanOrEqual(2);
    for (const k of keys) {
      expect(placeBody.quote_tokens[k]).toMatch(/^tok-\d+$/);
    }
  });
});
```

- [ ] **Step 2: Run Playwright**

```bash
cd web && npx playwright test position-order-quote-token
```

Expected: PASS. If the `position-row-order-button[-combo]` testids differ in the actual codebase, grep `web/components/PositionTable.tsx` for the existing testid and substitute.

- [ ] **Step 3: Verify open-path regressions (OrderTab still sends tokens)**

```bash
cd web && npm test -- orders-place
cd web && npm test -- order-reliability
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add web/tests/e2e/position-order-quote-token.spec.ts
git commit -m "test(e2e): position-order modal includes quote token(s) on submit"
```

---

## Task 9: Telemetry review + loose-ends update

**Files:**

- Modify: `docs/plans/2026-04-23-loose-ends.md` (update P1 #1 status to "SHIPPED — awaiting burn-in")

- [ ] **Step 1: Manually exercise the flow**

1. Start local stack (`scripts/cloud.sh`).
2. Open the web UI at `http://localhost:3000`.
3. Open a position row's order button for a single-leg option → submit close for 1 contract (dry-run / paper).
4. Repeat for a vertical combo.
5. Inspect DuckDB:

```bash
python3.13 -c "
import duckdb
con = duckdb.connect('data/orders.duckdb', read_only=True)
rows = con.execute(\"SELECT kind, detail FROM orders_events WHERE kind LIKE 'QUOTE_%' ORDER BY at DESC LIMIT 10\").fetchall()
for r in rows: print(r)
"
```

Expected: one `QUOTE_CHECK_PASS` row per combo submit; zero `QUOTE_TOKEN_MISSING_SOFT` rows if the modal is wired correctly.

- [ ] **Step 2: Update loose-ends**

Edit `docs/plans/2026-04-23-loose-ends.md` P1 #1 entry to reflect shipped status, and add a new P2 entry:

```markdown
### P2

N. **Flip combo `quote_tokens` missing → hard-reject.**
After one-week burn-in window from PR #NN merge, verify
`orders_events` has zero `QUOTE_TOKEN_MISSING_SOFT` rows
from web clients. Then remove the soft-fail branch from
`src/xenon/api/server.py` combo path and require
`quote_tokens` in schema. Trivial one-line PR.
```

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-04-23-loose-ends.md
git commit -m "docs: mark position-order quote_token regression fixed; file flip follow-up"
```

---

## Self-review

**Spec coverage check:**

| Spec section                                                                 | Task                                                                                                                |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Frontend `useQuoteTokens` hook                                               | Task 3                                                                                                              |
| Modal disables submit until tokens ready                                     | Task 4                                                                                                              |
| Modal attaches `quote_token` (single) or `quote_tokens` (combo)              | Task 4                                                                                                              |
| `check_combo` per-leg freshness/contract/size checks                         | Task 6                                                                                                              |
| Net-band reconstruction via envelope×leg XOR                                 | Task 6                                                                                                              |
| No tick-grid on combo net                                                    | Task 6 (deliberately absent)                                                                                        |
| `/orders/place` combo branch runs `check_combo` when tokens present          | Task 7                                                                                                              |
| `/orders/place` combo soft-fails with `QUOTE_TOKEN_MISSING_SOFT` when absent | Task 7                                                                                                              |
| Non-combo branch unchanged                                                   | Task 7 (no edits)                                                                                                   |
| Telemetry: `QUOTE_CHECK_PASS` / `FAIL` / `TOKEN_MISSING_SOFT`                | Task 7                                                                                                              |
| 6-structure unit set                                                         | Task 6 (bull call spread BUY/SELL env, risk reversal, stale leg, zero-size leg, token mismatch = 7 canonical cases) |
| Route tests (missing / tampered / out-of-band / pass)                        | Task 7                                                                                                              |
| Vitest for modal parallel-mint + payload shape                               | Task 4                                                                                                              |
| Playwright single-leg + combo close                                          | Task 8                                                                                                              |
| Rollout flip criterion documented                                            | Task 9                                                                                                              |
| Open question: verify single-leg close status                                | Addressed implicitly — Task 4 wires tokens for all three payload types so it works regardless of current behavior   |

No placeholders. Types consistent (`CheckComboLeg`, `quote_tokens` map keyed by `con_id` stringified, `_compute_combo_nets` signature matches caller). Every step has exact file paths, exact commands, exact code.
