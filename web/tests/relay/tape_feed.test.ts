import { describe, it, expect } from "vitest";
import {
  appendTrade,
  TAPE_RING_SIZE,
} from "../../../scripts/infra/ib_realtime/tape_feed.js";

describe("tape_feed.appendTrade", () => {
  it("appends newest-last and bounds to TAPE_RING_SIZE", () => {
    let ring = [];
    for (let i = 0; i < TAPE_RING_SIZE + 10; i++)
      ring = appendTrade(ring, {
        price: i,
        size: 1,
        exchange: "X",
        time: `${i}`,
      });
    expect(ring.length).toBe(TAPE_RING_SIZE);
    expect(ring[ring.length - 1].price).toBe(TAPE_RING_SIZE + 9);
    expect(ring[0].price).toBe(10);
  });
  it("grows without trimming while under the cap", () => {
    let ring = [];
    for (let i = 0; i < 5; i++)
      ring = appendTrade(ring, {
        price: i,
        size: 1,
        exchange: null,
        time: `${i}`,
      });
    expect(ring.length).toBe(5);
    expect(ring.map((t) => t.price)).toEqual([0, 1, 2, 3, 4]);
  });
  it("is immutable — does not mutate the input ring", () => {
    const ring = [{ price: 1, size: 1, exchange: null, time: "1" }];
    const next = appendTrade(ring, {
      price: 2,
      size: 1,
      exchange: null,
      time: "2",
    });
    expect(ring.length).toBe(1); // input untouched
    expect(next.length).toBe(2);
    expect(next).not.toBe(ring);
  });
});
