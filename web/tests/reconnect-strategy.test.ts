/**
 * @vitest-environment node
 *
 * Unit tests for the shared reconnect strategy utility.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createReconnectStrategy } from "../lib/reconnectStrategy";

beforeEach(() => {
  vi.spyOn(Math, "random").mockReturnValue(0.5);
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("createReconnectStrategy", () => {
  it("uses default config values", () => {
    const rs = createReconnectStrategy();
    expect(rs.attempt).toBe(0);
    expect(rs.canRetry()).toBe(true);
    // Default: base=1000, max=30000, jitter=500, maxAttempts=10
    // First delay = min(1000 * 2^0, 30000) + 0.5 * 500 = 1000 + 250 = 1250
    expect(rs.nextDelay()).toBe(1250);
  });

  it("exponential backoff doubles delay each attempt up to max", () => {
    const rs = createReconnectStrategy({ baseMs: 100, maxMs: 1000, jitterMs: 0 });
    // attempt 0: min(100 * 1, 1000) = 100
    expect(rs.nextDelay()).toBe(100);
    // attempt 1: min(100 * 2, 1000) = 200
    expect(rs.nextDelay()).toBe(200);
    // attempt 2: min(100 * 4, 1000) = 400
    expect(rs.nextDelay()).toBe(400);
    // attempt 3: min(100 * 8, 1000) = 800
    expect(rs.nextDelay()).toBe(800);
    // attempt 4: min(100 * 16, 1000) = 1000 (capped)
    expect(rs.nextDelay()).toBe(1000);
    // attempt 5: still capped at 1000
    expect(rs.nextDelay()).toBe(1000);
  });

  it("jitter adds randomness based on Math.random", () => {
    vi.spyOn(Math, "random").mockReturnValue(0.0);
    const rs1 = createReconnectStrategy({ baseMs: 1000, maxMs: 30000, jitterMs: 500 });
    expect(rs1.nextDelay()).toBe(1000); // 1000 + 0 * 500

    vi.spyOn(Math, "random").mockReturnValue(1.0);
    const rs2 = createReconnectStrategy({ baseMs: 1000, maxMs: 30000, jitterMs: 500 });
    expect(rs2.nextDelay()).toBe(1500); // 1000 + 1.0 * 500
  });

  it("maxAttempts enforcement: canRetry returns false after max", () => {
    const rs = createReconnectStrategy({ maxAttempts: 3, jitterMs: 0, baseMs: 100, maxMs: 10000 });
    expect(rs.canRetry()).toBe(true);
    rs.nextDelay(); // attempt 0 -> 1
    expect(rs.canRetry()).toBe(true);
    rs.nextDelay(); // attempt 1 -> 2
    expect(rs.canRetry()).toBe(true);
    rs.nextDelay(); // attempt 2 -> 3
    expect(rs.canRetry()).toBe(false);
  });

  it("unlimited attempts when maxAttempts is 0", () => {
    const rs = createReconnectStrategy({ maxAttempts: 0, jitterMs: 0, baseMs: 100, maxMs: 10000 });
    for (let i = 0; i < 100; i++) {
      expect(rs.canRetry()).toBe(true);
      rs.nextDelay();
    }
    expect(rs.canRetry()).toBe(true);
  });

  it("reset() resets attempt counter", () => {
    const rs = createReconnectStrategy({ maxAttempts: 3, jitterMs: 0, baseMs: 100, maxMs: 10000 });
    rs.nextDelay(); // attempt 0 -> 1
    rs.nextDelay(); // attempt 1 -> 2
    expect(rs.attempt).toBe(2);
    rs.reset();
    expect(rs.attempt).toBe(0);
    expect(rs.canRetry()).toBe(true);
    // After reset, first delay is back to base
    expect(rs.nextDelay()).toBe(100);
  });

  it("custom config is respected", () => {
    const rs = createReconnectStrategy({
      baseMs: 500,
      maxMs: 5000,
      maxAttempts: 5,
      jitterMs: 100,
    });
    // attempt 0: min(500 * 1, 5000) + 0.5 * 100 = 500 + 50 = 550
    expect(rs.nextDelay()).toBe(550);
    expect(rs.attempt).toBe(1);
  });

  it("attempt counter increments on each nextDelay call", () => {
    const rs = createReconnectStrategy({ jitterMs: 0, baseMs: 100, maxMs: 10000 });
    expect(rs.attempt).toBe(0);
    rs.nextDelay();
    expect(rs.attempt).toBe(1);
    rs.nextDelay();
    expect(rs.attempt).toBe(2);
    rs.nextDelay();
    expect(rs.attempt).toBe(3);
  });
});
