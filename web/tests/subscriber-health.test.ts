import { describe, it, expect } from "vitest";
import {
  classifySubscriber,
  formatAge,
  parseRealtimeSubscribers,
  DOT_CLASS,
} from "../lib/subscriberHealth";

describe("classifySubscriber", () => {
  it("live under 35s", () => {
    expect(
      classifySubscriber({ id: "a", connected: true, lastPongMsAgo: 3000 }),
    ).toBe("live");
  });
  it("stale between 35s and 65s", () => {
    expect(
      classifySubscriber({ id: "a", connected: true, lastPongMsAgo: 40_000 }),
    ).toBe("stale");
  });
  it("offline when not connected", () => {
    expect(
      classifySubscriber({ id: "a", connected: false, offlineForMs: 1000 }),
    ).toBe("offline");
  });
});

describe("formatAge", () => {
  it("seconds, minutes, hours", () => {
    expect(formatAge(3000)).toBe("3s");
    expect(formatAge(120_000)).toBe("2m");
    expect(formatAge(7_200_000)).toBe("2h");
    expect(formatAge(undefined)).toBe("—");
  });
});

describe("parseRealtimeSubscribers", () => {
  it("maps the /api/health block to camelCase", () => {
    const out = parseRealtimeSubscribers({
      realtime_subscribers: {
        reachable: true,
        anonymous_count: 2,
        subscribers: [
          { id: "a", connected: true, last_pong_ms_ago: 3000 },
          {
            id: "b",
            connected: false,
            offline_for_ms: 9000,
            last_seen_ms_ago: 9000,
          },
        ],
      },
    });
    expect(out.reachable).toBe(true);
    expect(out.anonymousCount).toBe(2);
    expect(out.subscribers[0]).toEqual({
      id: "a",
      connected: true,
      lastPongMsAgo: 3000,
    });
    expect(out.subscribers[1]).toEqual({
      id: "b",
      connected: false,
      offlineForMs: 9000,
      lastSeenMsAgo: 9000,
    });
  });
  it("returns an unreachable empty shape when the block is missing", () => {
    expect(parseRealtimeSubscribers({})).toEqual({
      reachable: false,
      subscribers: [],
      anonymousCount: 0,
    });
  });
});

describe("DOT_CLASS", () => {
  it("maps liveness to dot classes", () => {
    expect(DOT_CLASS.live).toBe("status-dot-live");
    expect(DOT_CLASS.stale).toBe("status-dot-stale");
    expect(DOT_CLASS.offline).toBe("status-dot-dead");
  });
});
