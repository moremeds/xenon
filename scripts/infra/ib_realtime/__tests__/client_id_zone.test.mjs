import { test } from "node:test";
import assert from "node:assert/strict";
import { relayClientIdBase, resolveRelayClientIds } from "../client_id_zone.js";

test("default relay ids derive from the base (prod 0 -> 10, dev 100 -> 110)", () => {
  assert.deepEqual(resolveRelayClientIds({ env: {} }), [10, 11, 12]);
  assert.deepEqual(resolveRelayClientIds({ env: { XENON_IB_CLIENT_ID_BASE: "0" } }), [10, 11, 12]);
  assert.deepEqual(
    resolveRelayClientIds({ env: { XENON_IB_CLIENT_ID_BASE: "100" } }),
    [110, 111, 112],
  );
});

test("explicit --client-id inside the relay zone is kept", () => {
  assert.deepEqual(
    resolveRelayClientIds({ env: { XENON_IB_CLIENT_ID_BASE: "100" }, cliClientId: 117 }),
    [117, 118, 119],
  );
});

test("relay refuses pool ids, Python ids and another base's ids", () => {
  const env = { XENON_IB_CLIENT_ID_BASE: "100" };
  for (const id of [104, 3, 4, 10, 118, 120, 126, 210]) {
    assert.throws(() => resolveRelayClientIds({ env, cliClientId: id }), /outside the relay zone/);
  }
});

test("invalid base values are refused", () => {
  for (const raw of ["50", "900", "-100", "abc", "1e2"]) {
    assert.throws(() => relayClientIdBase({ XENON_IB_CLIENT_ID_BASE: raw }));
  }
  assert.equal(relayClientIdBase({ XENON_IB_CLIENT_ID_BASE: " 800 " }), 800);
});
