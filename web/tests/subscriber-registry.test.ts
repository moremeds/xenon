import { describe, it, expect } from "vitest";
import { createSubscriberRegistry } from "../../scripts/infra/ib_realtime/subscriber_registry.js";

describe("subscriberRegistry", () => {
  it("reports a connected subscriber with small last_pong age", () => {
    const r = createSubscriberRegistry({ ttlMs: 1000 });
    r.onConnect("alpha", 1000);
    const snap = r.snapshot(1200);
    expect(snap).toEqual([
      {
        id: "alpha",
        connected: true,
        connected_at_ms: 1000,
        last_pong_ms_ago: 200,
      },
    ]);
  });

  it("advances last-seen on pong", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onPong("alpha", 5000);
    expect(r.snapshot(5200)[0].last_pong_ms_ago).toBe(200);
  });

  it("keeps a disconnected subscriber as offline with offline_for_ms", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onDisconnect("alpha", 2000);
    const s = r.snapshot(5000)[0];
    expect(s.connected).toBe(false);
    expect(s.offline_for_ms).toBe(3000);
  });

  it("stays connected when one of two same-id connections drops", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onConnect("alpha", 1100);
    r.onDisconnect("alpha", 2000);
    expect(r.snapshot(2100)[0].connected).toBe(true);
  });

  it("prunes entries older than ttl", () => {
    const r = createSubscriberRegistry({ ttlMs: 1000 });
    r.onConnect("alpha", 1000);
    r.onDisconnect("alpha", 1000);
    expect(r.snapshot(2500)).toEqual([]);
  });

  it("sorts subscribers by id", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("zeta", 1000);
    r.onConnect("alpha", 1000);
    expect(r.snapshot(1000).map((s) => s.id)).toEqual(["alpha", "zeta"]);
  });
});
