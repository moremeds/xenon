import { test } from "node:test";
import assert from "node:assert/strict";
import {
  API_KEY,
  OPTION,
  OPTION_KEY,
  sleep,
  startStack,
} from "./futu_stack.mjs";

// Subscription tiers (contract-x7): subscribe entries — symbol objects
// `{symbol, tier}` and contract objects — may carry `tier:"held"|"view"`;
// absent = held. The relay merges per symbol: the want tier is the highest
// over the symbol's subscribers (held > view) and goes out in `want.tiers`
// as `{sym: tier}`. A tier-only change must re-send `want`; an unsubscribe
// recomputes the tier over the remaining subscribers.

test("want.tiers: absent=held, declared view, invalid falls back to held", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      symbols: [
        { symbol: "AAPL", tier: "view" },
        "MSFT",
        { symbol: "JNK", tier: "bogus" },
      ],
    }),
  );
  await stack.waitFor(
    () => stack.wants.at(-1)?.symbols?.includes("JNK"),
  );
  const want = stack.wants.at(-1);
  assert.equal(want.tiers.AAPL, "view");
  assert.equal(want.tiers.MSFT, "held", "bare string entry must default held");
  assert.equal(want.tiers.JNK, "held", "unrecognized tier must default held");
  opted.close();
});

test("per-symbol tier = highest over subscribers; a second held client re-sends want", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const viewer = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  viewer.send(
    JSON.stringify({
      action: "subscribe",
      symbols: [{ symbol: "AAPL", tier: "view" }],
    }),
  );
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.AAPL === "view");

  // A second futu-first client with no tier (default held) subscribes the
  // same symbol: symbols unchanged, but the merged tier rises to held and a
  // new want gen goes out. Don't count generations exactly — the client's
  // first-health resend legitimately adds one — wait for a later want whose
  // merged tier is held.
  const holder = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  holder.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.AAPL === "held");
  assert.ok(stack.wants.at(-1).symbols.includes("AAPL"));
  viewer.close();
  holder.close();
});

test("tier change on unsubscribe: held subscriber leaving drops the symbol to view", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const viewer = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  viewer.send(
    JSON.stringify({
      action: "subscribe",
      symbols: [{ symbol: "AAPL", tier: "view" }],
    }),
  );
  const holder = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  holder.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.AAPL === "held");

  holder.send(
    JSON.stringify({ action: "unsubscribe", symbols: ["AAPL"] }),
  );
  // The symbol stays in want (viewer still subscribed) but its tier falls
  // back to view.
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.AAPL === "view");
  assert.ok(stack.wants.at(-1).symbols.includes("AAPL"));
  viewer.close();
  holder.close();
});

test("contract entries carry tier into want under XENON_FUTU_OPTIONS=1", async (t) => {
  const stack = await startStack({ XENON_FUTU_OPTIONS: "1" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      contracts: [{ ...OPTION, tier: "view" }],
    }),
  );
  await stack.waitFor(
    () => stack.wants.at(-1)?.symbols?.includes(OPTION_KEY),
  );
  assert.equal(stack.wants.at(-1).tiers[OPTION_KEY], "view");
  opted.close();
});

test("a held subscription from an IB-preference client still raises the tier", async (t) => {
  const stack = await startStack();
  t.after(stack.close);

  const viewer = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  viewer.send(
    JSON.stringify({
      action: "subscribe",
      symbols: [{ symbol: "QQQ", tier: "view" }],
    }),
  );
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.QQQ === "view");

  // The merge is over the symbol's subscribers, not only the futu-first ones:
  // a browser holding the position (no tier → held) protects the Futu
  // subscription the same way.
  const browser = await stack.connect({
    ticket: "t5",
    origin: "http://localhost:3200",
  });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["QQQ"] }));
  await stack.waitFor(() => stack.wants.at(-1)?.tiers?.QQQ === "held");
  viewer.close();
  browser.close();
});
