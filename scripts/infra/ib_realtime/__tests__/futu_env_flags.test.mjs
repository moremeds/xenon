import { test } from "node:test";
import assert from "node:assert/strict";
import {
  API_KEY,
  HD,
  OPTION,
  OPTION_KEY,
  sleep,
  startStack,
} from "./futu_stack.mjs";

// Relay env flags, read once at startup:
//   XENON_QUOTES_DEFAULT=ib|futu-first (default ib) — the quote preference for
//     EVERY client, browser ticket connections included. The api-key
//     ?quotes=futu-first opt-in is unchanged; there is no ?quotes=ib.
//   XENON_FUTU_OPTIONS=0|1 (default 0) — 0 keeps option contract keys pinned
//     to IB; 1 routes them like stocks and lets them into `want`. Indexes,
//     forex and non-US native venues stay pinned either way.
// Each test boots the real relay subprocess under its own env (futu_stack.mjs
// harness).

// ─── XENON_QUOTES_DEFAULT ─────────────────────────────────────────────────

test("XENON_QUOTES_DEFAULT=futu-first routes every client incl. tickets to Futu", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "futu-first" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t1",
    origin: "http://localhost:3200",
  });
  const apiPlain = await stack.connect({ apiKey: API_KEY });
  // There is no ?quotes=ib opt-out — an api-key client asking for it still
  // gets the env default.
  const apiIb = await stack.connect({ apiKey: API_KEY, quotes: "ib" });
  const all = [browser, apiPlain, apiIb];
  for (const ws of all) {
    ws.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  }

  await stack.admitOnFutu(["AAPL"]);
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { AAPL: { symbol: "AAPL", last: 11.5 } },
  });
  for (const ws of all) {
    await stack.waitFor(
      () => ws.batches().some((b) => b.updates?.AAPL?.last === 11.5),
      { timeout: 3000 },
    );
  }
  for (const ws of all) ws.close();
});

test("XENON_QUOTES_DEFAULT=ib keeps tickets on IB; api-key opt-in unchanged", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "ib" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t2",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["WMT"] }));
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["MSFT"] }));

  // The ticket client's symbol never enters want even though it asked for
  // futu-first; the api-key opt-in still does.
  await stack.admitOnFutu(["MSFT"]);
  await sleep(300);
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("WMT"), "ticket symbol leaked into want");
  }

  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: {
      MSFT: { symbol: "MSFT", last: 401 },
      WMT: { symbol: "WMT", last: 90 },
    },
  });
  await stack.waitFor(
    () => opted.batches().some((b) => b.updates?.MSFT?.last === 401),
    { timeout: 3000 },
  );
  await sleep(200);
  assert.equal(
    browser.batches().some((b) => b.updates?.WMT || b.updates?.MSFT),
    false,
    "ticket client received a futu tick under default=ib",
  );
  browser.close();
  opted.close();
});

test("unrecognized XENON_QUOTES_DEFAULT falls back to ib", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "garbage" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t3",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["XYZ"] }));
  await sleep(400);
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("XYZ"), "ticket symbol leaked into want");
  }
  browser.close();
});

// ─── XENON_FUTU_OPTIONS ──────────────────────────────────────────────────

test("XENON_FUTU_OPTIONS=0 keeps option keys pinned to IB", async (t) => {
  const stack = await startStack({ XENON_FUTU_OPTIONS: "0" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      symbols: ["SPY"],
      contracts: [OPTION],
    }),
  );

  // SPY enters want; the option key never does.
  await stack.admitOnFutu(["SPY"]);
  await sleep(300);
  for (const w of stack.wants) {
    assert.ok(
      !w.symbols.includes(OPTION_KEY),
      "option key leaked into want under XENON_FUTU_OPTIONS=0",
    );
  }

  // Even if the service volunteers the key in ok, the pinned key never
  // routes — same guard as the index test in futu_relay_e2e.
  stack.futuSend({
    type: "assign",
    gen: stack.wants.at(-1).gen,
    ok: ["SPY", OPTION_KEY],
    pending: [],
    rejected: [],
  });
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { [OPTION_KEY]: { symbol: OPTION_KEY, last: 5.5 } },
  });
  await sleep(300);
  assert.equal(
    opted.batches().some((b) => b.updates?.[OPTION_KEY]),
    false,
    "pinned option key received a futu tick",
  );
  opted.close();
});

test("XENON_FUTU_OPTIONS=1 sends option keys in want and routes their ticks", async (t) => {
  const stack = await startStack({ XENON_FUTU_OPTIONS: "1" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  const browser = await stack.connect({
    ticket: "t4",
    origin: "http://localhost:3200",
  });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      contracts: [OPTION],
      indexes: [{ symbol: "VIX", exchange: "CBOE" }],
      forexes: [{ base: "USD", quote: "JPY" }],
      stocks: [{ symbol: "7203", exchange: "TSEJ", currency: "JPY" }],
    }),
  );
  // A ticket client holding the same option keeps IB demand and stays on IB.
  browser.send(JSON.stringify({ action: "subscribe", contracts: [OPTION] }));

  // The option key enters want like a stock symbol; every other pin holds.
  await stack.waitFor(() =>
    stack.wants.at(-1)?.symbols?.includes(OPTION_KEY),
  );
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("VIX"), "index leaked into want");
    assert.ok(!w.symbols.includes("USD.JPY"), "forex leaked into want");
    assert.ok(!w.symbols.includes("7203"), "non-US stock leaked into want");
  }
  await stack.admitOnFutu([OPTION_KEY]);
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { [OPTION_KEY]: { symbol: OPTION_KEY, last: 5.5 } },
  });
  await stack.waitFor(
    () => opted.batches().some((b) => b.updates?.[OPTION_KEY]?.last === 5.5),
    { timeout: 3000 },
  );
  await sleep(200);
  assert.equal(
    browser.batches().some((b) => b.updates?.[OPTION_KEY]),
    false,
    "ib-preference client received the futu option tick",
  );
  opted.close();
  browser.close();
});
