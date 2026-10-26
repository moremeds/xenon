import { test } from "node:test";
import assert from "node:assert/strict";
import { API_KEY, HD, sleep, startStack } from "./futu_stack.mjs";

// Hold-down caching + stale (contract-x7 / D10):
//   - Futu batch rows for symbols in `ok` but not yet futu-live are CACHED,
//     not served; `futuLive` additionally requires a cached row, so a quiet
//     symbol keeps its IB line until a real Futu row exists (Fable F1).
//   - On the route switch the cached row is sent to the client (hydration).
//   - Futu-routed rows carry `stale` = now − timestamp > STALE_TICK_MS — the
//     same rule `withStale` applies to IB rows; a quiet overnight (X6)
//     symbol ages out by the same rule, no session carve-out.
//   - `providers.futu.routes` = per-client route counts {futu, ib}.

const lastStatus = (ws) => ws.statuses().at(-1)?.providers?.futu ?? null;

test("rows during hold-down are cached not served; hydration sends them on switch", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  await stack.admitOnFutu(["AAPL"]);

  // During the hold-down a first (e.g. OpenD snapshot) row arrives: cached,
  // not served.
  stack.futuSend({
    type: "batch",
    updates: {
      AAPL: { symbol: "AAPL", last: 11.0, timestamp: new Date().toISOString() },
    },
  });
  await sleep(300);
  assert.equal(
    opted.msgs.some((m) => m.symbol === "AAPL"),
    false,
    "hold-down row was served",
  );

  // Hold-down elapses with the row already cached → the route flips and the
  // client is hydrated with that row (previously dropped → frozen IB frame).
  await stack.waitFor(
    () =>
      opted.msgs.some(
        (m) =>
          m.type === "price" &&
          m.symbol === "AAPL" &&
          m.data?.last === 11.0 &&
          m.data?.source === "futu" &&
          m.data?.stale === false,
      ),
    { timeout: HD + 3000 },
  );
  opted.close();
});

test("F1: quiet symbol keeps the IB route until a Futu row exists", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["QUIET"] }));
  await stack.admitOnFutu(["QUIET"]);

  // Hold-down fully elapsed but no batch row ever arrived: the route stays
  // "ib" (the IB line is NOT dropped — futuLive needs a cached row).
  await sleep(HD + 400);
  await stack.waitFor(
    () => lastStatus(opted)?.routes?.ib === 1 && lastStatus(opted)?.routes?.futu === 0,
    { timeout: 3000 },
  );
  assert.equal(
    opted.msgs.some((m) => m.symbol === "QUIET"),
    false,
    "no futu frame should be emitted before a row exists",
  );

  // The first row lands after the hold-down: cached → immediately live →
  // hydrated to the client, and the route count flips to futu.
  stack.futuSend({
    type: "batch",
    updates: {
      QUIET: {
        symbol: "QUIET",
        last: 42.5,
        timestamp: new Date().toISOString(),
      },
    },
  });
  await stack.waitFor(
    () =>
      opted.msgs.some(
        (m) =>
          m.type === "price" &&
          m.symbol === "QUIET" &&
          m.data?.last === 42.5 &&
          m.data?.source === "futu",
      ),
    { timeout: 3000 },
  );
  await stack.waitFor(
    () => lastStatus(opted)?.routes?.futu === 1 && lastStatus(opted)?.routes?.ib === 0,
    { timeout: 3000 },
  );
  opted.close();
});

test("stale on futu rows: fresh row stale:false, aged timestamp stale:true", async (t) => {
  const stack = await startStack({ IB_REALTIME_STALE_TICK_MS: "500" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["MSFT"] }));
  await stack.admitOnFutu(["MSFT"]);
  await sleep(HD + 400);

  // The first row flips the route and reaches the client via the hydration
  // `price` frame — with a stale flag already stamped.
  stack.futuSend({
    type: "batch",
    updates: {
      MSFT: { symbol: "MSFT", last: 400.0, timestamp: new Date().toISOString() },
    },
  });
  await stack.waitFor(
    () =>
      opted.msgs.some(
        (m) =>
          m.type === "price" &&
          m.symbol === "MSFT" &&
          m.data?.last === 400.0 &&
          m.data?.stale === false,
      ),
    { timeout: 3000 },
  );

  stack.futuSend({
    type: "batch",
    updates: {
      MSFT: { symbol: "MSFT", last: 400.1, timestamp: new Date().toISOString() },
    },
  });
  await stack.waitFor(
    () =>
      opted
        .batches()
        .some((b) => b.updates?.MSFT?.last === 400.1 && b.updates?.MSFT?.stale === false),
    { timeout: 3000 },
  );

  // A row whose timestamp is older than STALE_TICK_MS is served with
  // stale:true — same age rule as IB's withStale.
  stack.futuSend({
    type: "batch",
    updates: {
      MSFT: {
        symbol: "MSFT",
        last: 400.2,
        timestamp: new Date(Date.now() - 5_000).toISOString(),
      },
    },
  });
  await stack.waitFor(
    () =>
      opted
        .batches()
        .some((b) => b.updates?.MSFT?.last === 400.2 && b.updates?.MSFT?.stale === true),
    { timeout: 3000 },
  );
  opted.close();
});

test("quiet Futu row flips to stale on the reconcile timer without a new tick", async (t) => {
  const stack = await startStack({ IB_REALTIME_STALE_TICK_MS: "500" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["SYNQUIET"] }));
  await stack.admitOnFutu(["SYNQUIET"]);
  await sleep(HD + 400);

  // One fresh row, then only health frames — the row ages past the threshold
  // with no new tick, and the reconcile timer must publish the stale flip.
  const stamp = new Date().toISOString();
  stack.futuSend({
    type: "batch",
    updates: {
      SYNQUIET: { symbol: "SYNQUIET", last: 12.34, timestamp: stamp },
    },
  });
  await stack.waitFutuValue(opted, "SYNQUIET", 12.34, { timeout: 3000 });

  await stack.waitFor(
    () =>
      opted.msgs.some(
        (m) =>
          (m.updates?.SYNQUIET?.stale === true &&
            m.updates?.SYNQUIET?.last === 12.34 &&
            m.updates?.SYNQUIET?.timestamp === stamp) ||
          (m.type === "price" &&
            m.symbol === "SYNQUIET" &&
            m.data?.stale === true &&
            m.data?.timestamp === stamp),
      ),
    { timeout: 5000 },
  );
  opted.close();
});

test("hydrated cached row carries stale:true once its timestamp ages out", async (t) => {
  const stack = await startStack({ IB_REALTIME_STALE_TICK_MS: "200" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["SPY"] }));
  await stack.admitOnFutu(["SPY"]);

  // Row cached during hold-down; by the time the route flips (HD=400 >
  // STALE=300) its timestamp is already past the threshold.
  stack.futuSend({
    type: "batch",
    updates: {
      SPY: { symbol: "SPY", last: 500.5, timestamp: new Date().toISOString() },
    },
  });
  await stack.waitFor(
    () =>
      opted.msgs.some(
        (m) =>
          m.type === "price" &&
          m.symbol === "SPY" &&
          m.data?.last === 500.5 &&
          m.data?.stale === true,
      ),
    { timeout: HD + 3000 },
  );
  opted.close();
});
