import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeForex, normalizeStocksMeta } from "../normalize.js";

test("normalizeForex keeps valid base/quote pairs and keys them", () => {
  const out = normalizeForex([
    { base: "usd", quote: "jpy" },
    { base: "USD", quote: "" },
    "bad",
    null,
  ]);
  assert.deepEqual(out, [{ base: "USD", quote: "JPY", key: "USD.JPY" }]);
});

test("normalizeForex returns [] for non-array input", () => {
  assert.deepEqual(normalizeForex(undefined), []);
  assert.deepEqual(normalizeForex({ base: "USD", quote: "JPY" }), []);
});

test("normalizeStocksMeta requires symbol+exchange+currency", () => {
  const out = normalizeStocksMeta([
    { symbol: "5016", exchange: "tsej", currency: "jpy" },
    { symbol: "000660", exchange: "KRX", currency: "KRW" },
    { symbol: "AAPL", exchange: "", currency: "USD" }, // missing exchange → drop
    "bad",
  ]);
  assert.deepEqual(out, [
    { symbol: "5016", exchange: "TSEJ", currency: "JPY" },
    { symbol: "000660", exchange: "KRX", currency: "KRW" },
  ]);
});
