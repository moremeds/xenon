import { describe, it, expect } from "vitest";
import { LRUCache } from "../../scripts/lib/lru-cache.js";

describe("LRUCache", () => {
  describe("basic get/set/has/delete", () => {
    it("stores and retrieves a value", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      expect(cache.get("a")).toBe(1);
    });

    it("returns undefined for missing keys", () => {
      const cache = new LRUCache(10);
      expect(cache.get("missing")).toBeUndefined();
    });

    it("has() returns true for existing keys", () => {
      const cache = new LRUCache(10);
      cache.set("x", 42);
      expect(cache.has("x")).toBe(true);
      expect(cache.has("y")).toBe(false);
    });

    it("delete() removes a key", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      cache.delete("a");
      expect(cache.has("a")).toBe(false);
      expect(cache.get("a")).toBeUndefined();
      expect(cache.size).toBe(0);
    });

    it("tracks size correctly", () => {
      const cache = new LRUCache(10);
      expect(cache.size).toBe(0);
      cache.set("a", 1);
      cache.set("b", 2);
      expect(cache.size).toBe(2);
    });

    it("clear() empties the cache", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.clear();
      expect(cache.size).toBe(0);
      expect(cache.has("a")).toBe(false);
    });

    it("overwrites existing key with set()", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      cache.set("a", 2);
      expect(cache.get("a")).toBe(2);
      expect(cache.size).toBe(1);
    });
  });

  describe("LRU eviction", () => {
    it("evicts oldest entry when max exceeded", () => {
      const cache = new LRUCache(3);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.set("c", 3);
      // At capacity — adding one more evicts "a"
      cache.set("d", 4);
      expect(cache.has("a")).toBe(false);
      expect(cache.has("b")).toBe(true);
      expect(cache.has("c")).toBe(true);
      expect(cache.has("d")).toBe(true);
      expect(cache.size).toBe(3);
    });

    it("does not evict when exactly at max", () => {
      const cache = new LRUCache(3);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.set("c", 3);
      expect(cache.size).toBe(3);
      expect(cache.has("a")).toBe(true);
      expect(cache.has("b")).toBe(true);
      expect(cache.has("c")).toBe(true);
    });

    it("evicts multiple times correctly", () => {
      const cache = new LRUCache(2);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.set("c", 3); // evicts "a"
      cache.set("d", 4); // evicts "b"
      expect(cache.has("a")).toBe(false);
      expect(cache.has("b")).toBe(false);
      expect(cache.get("c")).toBe(3);
      expect(cache.get("d")).toBe(4);
    });
  });

  describe("access-order promotion", () => {
    it("get() promotes item to most recent (prevents eviction)", () => {
      const cache = new LRUCache(3);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.set("c", 3);
      // Access "a" to promote it
      cache.get("a");
      // Now "b" is the oldest — adding "d" evicts "b"
      cache.set("d", 4);
      expect(cache.has("a")).toBe(true); // promoted, not evicted
      expect(cache.has("b")).toBe(false); // oldest, evicted
      expect(cache.has("c")).toBe(true);
      expect(cache.has("d")).toBe(true);
    });

    it("set() on existing key promotes it", () => {
      const cache = new LRUCache(3);
      cache.set("a", 1);
      cache.set("b", 2);
      cache.set("c", 3);
      // Update "a" to promote it
      cache.set("a", 10);
      // Now "b" is the oldest
      cache.set("d", 4);
      expect(cache.has("a")).toBe(true);
      expect(cache.get("a")).toBe(10);
      expect(cache.has("b")).toBe(false);
    });
  });

  describe("iteration", () => {
    it("values() returns all stored values", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      cache.set("b", 2);
      const vals = [...cache.values()];
      expect(vals).toEqual([1, 2]);
    });

    it("entries() returns all key-value pairs", () => {
      const cache = new LRUCache(10);
      cache.set("x", 10);
      cache.set("y", 20);
      const entries = [...cache.entries()];
      expect(entries).toEqual([
        ["x", 10],
        ["y", 20],
      ]);
    });

    it("Symbol.iterator works with for...of", () => {
      const cache = new LRUCache(10);
      cache.set("a", 1);
      cache.set("b", 2);
      const collected: [string, number][] = [];
      for (const entry of cache) {
        collected.push(entry as [string, number]);
      }
      expect(collected).toEqual([
        ["a", 1],
        ["b", 2],
      ]);
    });
  });
});
