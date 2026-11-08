// Risk-guard P0 finding (docs/research/2026-11-08-risk-guard-p0.md §1): on
// 2026-11-03..05 IB sent no SPX ticks in RTH and the relay published the prior
// close as `last`. These cover the two relay-side fixes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createPriceData, maskStaleIndexLast } from "../ib_tick_handler.js";
import { staleRestartCooldownMs } from "../ib_connection_status.js";

function snap(symbol, { last = 7688.62, stale = true, lastIsCalculated = true } = {}) {
  return { ...createPriceData(symbol), last, close: 7688.62, stale, lastIsCalculated };
}

test("RTH + stale cash index: last is withheld, stale stays true", () => {
  const out = maskStaleIndexLast(snap("SPX"), true);
  assert.equal(out.last, null);
  assert.equal(out.stale, true);
  assert.equal(out.close, 7688.62);
});

test("RTH + stale index with an old real print is withheld too", () => {
  assert.equal(maskStaleIndexLast(snap("VIX", { lastIsCalculated: false }), true).last, null);
});

test("fresh index line passes through", () => {
  const s = snap("SPX", { stale: false, lastIsCalculated: false });
  assert.equal(maskStaleIndexLast(s, true), s);
});

test("outside RTH the index close is the latest value and stays", () => {
  const s = snap("SPX");
  assert.equal(maskStaleIndexLast(s, false), s);
});

test("slow indexes keep their last: a flat live line can look stale", () => {
  for (const sym of ["MOVE", "OVX", "COR1M", "VVIX"]) {
    const s = snap(sym, { lastIsCalculated: false });
    assert.equal(maskStaleIndexLast(s, true), s);
  }
});

test("stocks and index options are untouched", () => {
  const stock = snap("SPY");
  assert.equal(maskStaleIndexLast(stock, true), stock);
  const opt = snap("SPX_20261120_7500_P");
  assert.equal(maskStaleIndexLast(opt, true), opt);
});

test("the input snapshot is not mutated", () => {
  const s = snap("SPX");
  maskStaleIndexLast(s, true);
  assert.equal(s.last, 7688.62);
});

test("stale-restart cooldown backs off and caps", () => {
  assert.equal(staleRestartCooldownMs(1), 120_000);
  assert.equal(staleRestartCooldownMs(2), 240_000);
  assert.equal(staleRestartCooldownMs(3), 480_000);
  assert.equal(staleRestartCooldownMs(10), 1_800_000);
  assert.equal(staleRestartCooldownMs(0), 120_000);
});
