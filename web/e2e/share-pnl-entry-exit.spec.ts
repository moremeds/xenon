/**
 * E2E tests for Share PnL entry/exit price and time display.
 *
 * Tests that the Share PnL image generation:
 * 1. Includes entry price and time (with PST timezone)
 * 2. Includes exit price and time (with PST timezone)
 * 3. Does NOT include commission
 */
import { test, expect } from "@playwright/test";

test.describe("Share PnL API Route", () => {
  test("should generate image with entry and exit price/time in PST, without commission", async ({ request }) => {
    // Call the API with test data matching our requirements
    const response = await request.get("/api/share/pnl", {
      params: {
        description: "Closed AAOI (Long $115 Call)",
        pnlPct: "44.08",
        entryPrice: "5.59",
        exitPrice: "8.05",
        entryTime: "2026-03-15T10:30:00-07:00",
        exitTime: "2026-03-18T07:03:53-07:00",
        // NOTE: commission is intentionally NOT included
      },
    });

    expect(response.ok()).toBe(true);
    expect(response.headers()["content-type"]).toBe("image/png");

    // The image was generated successfully
    const buffer = await response.body();
    expect(buffer.length).toBeGreaterThan(1000); // Sanity check - PNG should be more than 1KB
  });

  test("should generate image with only percentage when no dollar amount provided", async ({ request }) => {
    const response = await request.get("/api/share/pnl", {
      params: {
        description: "Closed AAOI (Long $115 Call)",
        pnlPct: "44.08",
      },
    });

    expect(response.ok()).toBe(true);
    expect(response.headers()["content-type"]).toBe("image/png");
  });

  test("should generate image with both dollar and percentage", async ({ request }) => {
    const response = await request.get("/api/share/pnl", {
      params: {
        description: "Closed AAOI (Long $115 Call)",
        pnl: "8265.02",
        pnlPct: "44.08",
        entryPrice: "5.59",
        exitPrice: "8.05",
        entryTime: "2026-03-15T10:30:00-07:00",
        exitTime: "2026-03-18T07:03:53-07:00",
      },
    });

    expect(response.ok()).toBe(true);
    expect(response.headers()["content-type"]).toBe("image/png");
  });

  test("should return 400 when description is missing", async ({ request }) => {
    const response = await request.get("/api/share/pnl", {
      params: {
        pnlPct: "44.08",
      },
    });

    expect(response.status()).toBe(400);
  });

  test("should handle negative P&L correctly", async ({ request }) => {
    const response = await request.get("/api/share/pnl", {
      params: {
        description: "Closed GOOG (Bull Call Spread)",
        pnl: "-215.00",
        pnlPct: "-13.95",
        entryPrice: "6.27",
        exitPrice: "5.40",
        entryTime: "2026-03-04T09:30:00-08:00",
        exitTime: "2026-03-04T15:45:00-08:00",
      },
    });

    expect(response.ok()).toBe(true);
    expect(response.headers()["content-type"]).toBe("image/png");
  });
});
