/**
 * Order E2E Tests
 *
 * Higher-level integration tests for the complete order flow.
 * Tests the interaction between frontend components, API routes, and backend scripts.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { createHmac } from "node:crypto";
import { NextRequest } from "next/server";
import { ensureTestFastApi } from "./fastapiHarness";

const fastApiHarness = await ensureTestFastApi();
const fastApiIt = fastApiHarness.available ? it : it.skip;

if (!fastApiHarness.available && fastApiHarness.skipReason) {
  console.warn(
    `[order-e2e] Skipping FastAPI-backed order tests: ${fastApiHarness.skipReason}`,
  );
}

afterAll(async () => {
  await fastApiHarness.close();
});

const QUOTE_SECRET =
  "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

function b64url(data: Buffer | string) {
  return Buffer.from(data).toString("base64url");
}

function quoteToken({
  conId,
  ticker = "AAPL",
  bid,
  ask,
}: {
  conId: number;
  ticker?: string;
  bid: string;
  ask: string;
}) {
  const body = JSON.stringify(
    {
      ask,
      ask_size: 10,
      bid,
      bid_size: 10,
      con_id: conId,
      ticker,
      ts_server_ms: Date.now(),
    },
    Object.keys({
      ask,
      ask_size: 10,
      bid,
      bid_size: 10,
      con_id: conId,
      ticker,
      ts_server_ms: 0,
    }).sort(),
  );
  const sig = createHmac("sha256", QUOTE_SECRET).update(body).digest();
  return `${b64url(body)}.${b64url(sig)}`;
}

// ---------------------------------------------------------------------------
// Place route — IB rejection handling
// ---------------------------------------------------------------------------

// FastAPI-backed integration tests. The harness spawns a real uvicorn process
// in test mode; under full-suite parallel pressure its startup + per-request
// latency can push past vitest's 5s default. These are skipped when the
// harness is unavailable (no .venv), so this timeout only matters when the
// suite is running against a real FastAPI.
describe(
  "POST /api/orders/place — IB rejection detection",
  { timeout: 15_000 },
  () => {
    let placePOST: (req: Request) => Promise<Response>;

    beforeAll(async () => {
      const mod = await import("../app/api/orders/place/route");
      placePOST = mod.POST;
    });

    fastApiIt(
      "accepts valid stock order payload (requires FastAPI)",
      async () => {
        const req = new NextRequest("http://localhost/api/orders/place", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "stock",
            symbol: "SPY",
            action: "BUY",
            quantity: 1,
            limitPrice: 500.0,
            tif: "DAY",
            client_attempt_id: "order-e2e-stock-1",
            con_id: 1001,
            quote_token: quoteToken({
              conId: 1001,
              ticker: "SPY",
              bid: "499.99",
              ask: "500.01",
            }),
          }),
        });
        const res = await placePOST(req);
        expect(res.status).toBe(200);
      },
    );

    fastApiIt(
      "accepts valid option order payload (requires FastAPI)",
      async () => {
        const req = new NextRequest("http://localhost/api/orders/place", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "option",
            symbol: "QQQ",
            action: "BUY",
            quantity: 1,
            limitPrice: 5.0,
            tif: "GTC",
            expiry: "20260417",
            strike: 200,
            right: "C",
            client_attempt_id: "order-e2e-option-1",
            con_id: 2001,
            quote_token: quoteToken({
              conId: 2001,
              ticker: "QQQ",
              bid: "4.99",
              ask: "5.01",
            }),
          }),
        });
        const res = await placePOST(req);
        expect(res.status).toBe(200);
      },
    );

    fastApiIt(
      "accepts valid combo order payload (requires FastAPI)",
      async () => {
        const req = new NextRequest("http://localhost/api/orders/place", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "combo",
            symbol: "QQQ",
            action: "BUY",
            quantity: 1,
            limitPrice: 2.5,
            tif: "GTC",
            legs: [
              {
                expiry: "20260417",
                strike: 200,
                right: "C",
                action: "BUY",
                ratio: 1,
              },
              {
                expiry: "20260417",
                strike: 210,
                right: "C",
                action: "SELL",
                ratio: 1,
              },
            ],
            client_attempt_id: "order-e2e-combo-1",
          }),
        });
        const res = await placePOST(req);
        expect(res.status).toBe(200);
      },
    );

    fastApiIt(
      "preserves TIF default to DAY when not specified (requires FastAPI)",
      async () => {
        const req = new NextRequest("http://localhost/api/orders/place", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "stock",
            symbol: "SPY",
            action: "BUY",
            quantity: 1,
            limitPrice: 500.0,
            client_attempt_id: "order-e2e-stock-default-tif-1",
            con_id: 1002,
            quote_token: quoteToken({
              conId: 1002,
              ticker: "SPY",
              bid: "499.99",
              ask: "500.01",
            }),
            // tif not specified
          }),
        });
        const res = await placePOST(req);
        expect(res.status).toBe(200);
      },
    );
  },
);

// ---------------------------------------------------------------------------
// Modify route — combo replacement
// ---------------------------------------------------------------------------

describe(
  "POST /api/orders/modify — combo replacement",
  { timeout: 15_000 },
  () => {
    let modifyPOST: (req: Request) => Promise<Response>;

    beforeAll(async () => {
      const mod = await import("../app/api/orders/modify/route");
      modifyPOST = mod.POST;
    });

    fastApiIt(
      "accepts valid combo replacement (requires FastAPI)",
      async () => {
        const req = new NextRequest("http://localhost/api/orders/modify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            permId: 12345,
            replaceOrder: {
              type: "combo",
              symbol: "QQQ",
              action: "SELL",
              quantity: 1,
              limitPrice: 3.0,
              tif: "GTC",
              legs: [
                {
                  expiry: "20260417",
                  strike: 200,
                  right: "C",
                  action: "BUY",
                  ratio: 1,
                },
                {
                  expiry: "20260417",
                  strike: 210,
                  right: "C",
                  action: "SELL",
                  ratio: 1,
                },
              ],
              client_attempt_id: "order-e2e-combo-replace-1",
            },
          }),
        });
        const res = await modifyPOST(req);
        expect(res.status).toBe(200);
      },
    );

    it("rejects combo replacement with missing limitPrice", async () => {
      const req = new NextRequest("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          permId: 12345,
          replaceOrder: {
            type: "combo",
            symbol: "AAPL",
            action: "SELL",
            quantity: 10,
            // limitPrice missing
            legs: [
              {
                expiry: "20260417",
                strike: 200,
                right: "C",
                action: "BUY",
                ratio: 1,
              },
              {
                expiry: "20260417",
                strike: 210,
                right: "C",
                action: "SELL",
                ratio: 1,
              },
            ],
          },
        }),
      });
      const res = await modifyPOST(req);
      expect(res.status).toBe(400);
    });

    it("rejects combo replacement with wrong type", async () => {
      const req = new NextRequest("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          permId: 12345,
          replaceOrder: {
            type: "option", // should be "combo"
            symbol: "AAPL",
            action: "SELL",
            quantity: 10,
            limitPrice: 3.0,
            legs: [
              {
                expiry: "20260417",
                strike: 200,
                right: "C",
                action: "BUY",
                ratio: 1,
              },
              {
                expiry: "20260417",
                strike: 210,
                right: "C",
                action: "SELL",
                ratio: 1,
              },
            ],
          },
        }),
      });
      const res = await modifyPOST(req);
      expect(res.status).toBe(400);
    });
  },
);

// ---------------------------------------------------------------------------
// Cancel route — edge cases
// ---------------------------------------------------------------------------

describe("POST /api/orders/cancel — edge cases", () => {
  let cancelPOST: (req: Request) => Promise<Response>;

  beforeAll(async () => {
    const mod = await import("../app/api/orders/cancel/route");
    cancelPOST = mod.POST;
  });

  fastApiIt("accepts cancel by permId only (requires FastAPI)", async () => {
    const req = new NextRequest("http://localhost/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ permId: 12345 }),
    });
    const res = await cancelPOST(req);
    expect(res.status).toBe(200);
  });

  fastApiIt("accepts cancel by orderId only (requires FastAPI)", async () => {
    const req = new NextRequest("http://localhost/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId: 42 }),
    });
    const res = await cancelPOST(req);
    expect(res.status).toBe(200);
  });

  fastApiIt(
    "accepts cancel by both orderId and permId (requires FastAPI)",
    async () => {
      const req = new NextRequest("http://localhost/api/orders/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orderId: 42, permId: 12345 }),
      });
      const res = await cancelPOST(req);
      expect(res.status).toBe(200);
    },
  );
});

// ---------------------------------------------------------------------------
// Order payload normalization
// ---------------------------------------------------------------------------

import { normalizeOptionExpiry } from "../lib/pricesProtocol";

describe("normalizeOptionExpiry", () => {
  it("returns YYYYMMDD for already-clean format", () => {
    expect(normalizeOptionExpiry("20260417")).toBe("20260417");
  });

  it("strips dashes from YYYY-MM-DD", () => {
    expect(normalizeOptionExpiry("2026-04-17")).toBe("20260417");
  });

  it("returns null for invalid format", () => {
    expect(normalizeOptionExpiry("04/17/2026")).toBeNull();
    expect(normalizeOptionExpiry("Apr 17, 2026")).toBeNull();
    expect(normalizeOptionExpiry("")).toBeNull();
  });

  it("handles whitespace in expiry", () => {
    expect(normalizeOptionExpiry(" 20260417 ")).toBe("20260417");
    expect(normalizeOptionExpiry(" 2026-04-17 ")).toBe("20260417");
  });
});

// ---------------------------------------------------------------------------
// optionKey consistency
// ---------------------------------------------------------------------------

import { optionKey, normalizeOptionContract } from "../lib/pricesProtocol";

describe("optionKey format consistency", () => {
  it("produces consistent keys for same contract", () => {
    const key1 = optionKey({
      symbol: "AAPL",
      expiry: "20260417",
      strike: 200,
      right: "C",
    });
    const key2 = optionKey({
      symbol: "AAPL",
      expiry: "20260417",
      strike: 200,
      right: "C",
    });
    expect(key1).toBe(key2);
  });

  it("normalizes symbol to uppercase", () => {
    const key = optionKey({
      symbol: "aapl",
      expiry: "20260417",
      strike: 200,
      right: "C",
    });
    expect(key).toBe("AAPL_20260417_200_C");
  });

  it("uses underscore separator", () => {
    const key = optionKey({
      symbol: "AAPL",
      expiry: "20260417",
      strike: 200,
      right: "C",
    });
    expect(key).toMatch(/^AAPL_\d+_\d+_C$/);
  });

  it("normalizes YYYY-MM-DD expiry to YYYYMMDD", () => {
    const key = optionKey({
      symbol: "AAPL",
      expiry: "2026-04-17",
      strike: 200,
      right: "C",
    });
    expect(key).toBe("AAPL_20260417_200_C");
  });
});

// ---------------------------------------------------------------------------
// Structure detection edge cases
// ---------------------------------------------------------------------------

import { detectStructure, type OrderLeg } from "../lib/optionsChainUtils";

describe("detectStructure edge cases", () => {
  it("handles empty legs", () => {
    expect(detectStructure([])).toBe("");
  });

  it("detects 3+ leg combos", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "C",
        strike: 100,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
      {
        id: "2",
        action: "SELL",
        right: "C",
        strike: 105,
        expiry: "20260417",
        quantity: 2,
        limitPrice: null,
      },
      {
        id: "3",
        action: "BUY",
        right: "C",
        strike: 110,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
    ];
    expect(detectStructure(legs)).toBe("3-Leg Combo");
  });

  it("handles 4-leg iron condor pattern", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "P",
        strike: 90,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
      {
        id: "2",
        action: "SELL",
        right: "P",
        strike: 95,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
      {
        id: "3",
        action: "SELL",
        right: "C",
        strike: 105,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
      {
        id: "4",
        action: "BUY",
        right: "C",
        strike: 110,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
    ];
    expect(detectStructure(legs)).toBe("4-Leg Combo");
  });

  it("handles diagonal spread (different expiries)", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "C",
        strike: 100,
        expiry: "20260520",
        quantity: 1,
        limitPrice: null,
      },
      {
        id: "2",
        action: "SELL",
        right: "C",
        strike: 105,
        expiry: "20260417",
        quantity: 1,
        limitPrice: null,
      },
    ];
    // Different expiries — should be Calendar Spread
    expect(detectStructure(legs)).toBe("Calendar Spread");
  });
});

// ---------------------------------------------------------------------------
// GCD normalization for ratio spreads
// ---------------------------------------------------------------------------

import { normalizeComboOrder } from "../lib/optionsChainUtils";

describe("normalizeComboOrder GCD edge cases", () => {
  it("handles quantity of 0 (treats as 1)", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "C",
        strike: 100,
        expiry: "20260417",
        quantity: 0,
        limitPrice: null,
      },
    ];
    const result = normalizeComboOrder(legs);
    expect(result.quantity).toBe(1);
    expect(result.legs[0].quantity).toBe(1);
  });

  it("handles large quantities (1000x)", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "C",
        strike: 100,
        expiry: "20260417",
        quantity: 1000,
        limitPrice: null,
      },
      {
        id: "2",
        action: "SELL",
        right: "C",
        strike: 110,
        expiry: "20260417",
        quantity: 1000,
        limitPrice: null,
      },
    ];
    const result = normalizeComboOrder(legs);
    expect(result.quantity).toBe(1000);
    expect(result.legs[0].quantity).toBe(1);
    expect(result.legs[1].quantity).toBe(1);
  });

  it("handles asymmetric ratio (3:5)", () => {
    const legs: OrderLeg[] = [
      {
        id: "1",
        action: "BUY",
        right: "C",
        strike: 100,
        expiry: "20260417",
        quantity: 30,
        limitPrice: null,
      },
      {
        id: "2",
        action: "SELL",
        right: "C",
        strike: 110,
        expiry: "20260417",
        quantity: 50,
        limitPrice: null,
      },
    ];
    const result = normalizeComboOrder(legs);
    expect(result.quantity).toBe(10);
    expect(result.legs[0].quantity).toBe(3);
    expect(result.legs[1].quantity).toBe(5);
  });
});
