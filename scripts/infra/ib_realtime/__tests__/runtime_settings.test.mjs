import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createRuntimeSettings,
  parseOverrides,
  relayEnvValues,
  settingsUrl,
} from "../runtime_settings.js";
import { linesToShed } from "../line_budget.js";
import {
  HD,
  INTERNAL_TOKEN,
  OPTION,
  OPTION_KEY,
  sleep,
  startStack,
} from "./futu_stack.mjs";

// Runtime settings (D5/D6): the relay pulls DB overrides from the api
// (GET /admin/settings, X-Internal-Token), applies its own keys live and
// forwards the futu-quotes keys in a `config` frame. api unreachable → last
// effective values stay (start: env).

const row = (key, db_value) => ({ key, db_value });

// ─── pure helpers ─────────────────────────────────────────────────────────

test("settingsUrl uses the ticket validate origin", () => {
  assert.equal(
    settingsUrl("http://api:8321/ws-ticket/validate"),
    "http://api:8321/admin/settings",
  );
});

test("relayEnvValues keeps today's env parsing", () => {
  assert.deepEqual(relayEnvValues({}), {
    XENON_QUOTES_DEFAULT: "ib",
    XENON_FUTU_OPTIONS: false,
    IB_REALTIME_MAX_LINES: 85,
    IB_REALTIME_STALE_TICK_MS: 60_000,
  });
  assert.deepEqual(
    relayEnvValues({
      XENON_QUOTES_DEFAULT: "futu-first",
      XENON_FUTU_OPTIONS: "1",
      IB_REALTIME_MAX_LINES: "3",
      IB_REALTIME_STALE_TICK_MS: "5000",
    }),
    {
      XENON_QUOTES_DEFAULT: "futu-first",
      XENON_FUTU_OPTIONS: true,
      IB_REALTIME_MAX_LINES: 10,
      IB_REALTIME_STALE_TICK_MS: 5000,
    },
  );
});

test("parseOverrides keeps valid relay/futu-quotes rows only", () => {
  const out = parseOverrides({
    settings: [
      row("XENON_QUOTES_DEFAULT", "futu-first"),
      row("XENON_FUTU_OPTIONS", "1"), // not a bool → ignored
      row("IB_REALTIME_MAX_LINES", 9), // below floor → ignored
      row("IB_REALTIME_STALE_TICK_MS", null), // no override
      row("FUTU_QUOTE_QUOTA", 400),
      row("FUTU_GREEK_FACTORS", { iv: 0.01, rho: 1 }), // unknown greek → ignored
      row("FUTU_BOOK_STALE_S", 2.5),
      row("orders_paused", true), // api key → not the relay's
    ],
  });
  assert.deepEqual(out, {
    XENON_QUOTES_DEFAULT: "futu-first",
    FUTU_QUOTE_QUOTA: 400,
    FUTU_BOOK_STALE_S: 2.5,
  });
  assert.equal(parseOverrides({}), null);
  assert.equal(parseOverrides(null), null);
});

test("linesToShed evicts view lines first, then the idlest held", () => {
  const entries = [
    { key: "H1", tickerId: 1, lastAccessAt: 10, tier: "held" },
    { key: "H2", tickerId: 2, lastAccessAt: 50, tier: "held" },
    { key: "V1", tickerId: 3, lastAccessAt: 90, tier: "view" },
    { key: "V2", tickerId: 4, lastAccessAt: 20, tier: "view" },
    { key: "P", tickerId: null, lastAccessAt: 99, tier: "held" },
  ];
  assert.deepEqual(linesToShed(entries, 4), []);
  assert.deepEqual(linesToShed(entries, 2), ["V2", "V1"]);
  assert.deepEqual(linesToShed(entries, 1), ["V2", "V1", "H1"]);
});

// ─── poller (fake fetch) ──────────────────────────────────────────────────

function fakeFetch() {
  const f = async (url, opts) => {
    f.calls.push({ url, headers: opts.headers });
    if (f.fail) throw new Error("ECONNREFUSED");
    return {
      ok: f.status === 200,
      status: f.status,
      json: async () => ({ settings: f.rows }),
    };
  };
  f.calls = [];
  f.rows = [];
  f.status = 200;
  f.fail = false;
  return f;
}

test("poller: override → change callback; failure keeps last values", async () => {
  const fetchImpl = fakeFetch();
  const relayChanges = [];
  const futuConfigs = [];
  const rs = createRuntimeSettings({
    url: "http://api/admin/settings",
    token: "tok",
    env: relayEnvValues({}),
    fetchImpl,
    onRelayChange: (eff, prev) => relayChanges.push({ eff, prev }),
    onFutuConfig: (v) => futuConfigs.push(v),
  });
  assert.equal(rs.futuConfig(), null); // nothing known before the first pull

  assert.equal(await rs.pollOnce(), true);
  assert.equal(fetchImpl.calls[0].headers["X-Internal-Token"], "tok");
  assert.equal(fetchImpl.calls[0].headers["X-API-Key"], undefined);
  assert.equal(relayChanges.length, 0); // no rows = env values
  assert.equal(futuConfigs.length, 1); // first known config is forwarded
  assert.equal(futuConfigs[0].FUTU_QUOTE_QUOTA, null);

  fetchImpl.rows = [
    row("IB_REALTIME_MAX_LINES", 40),
    row("FUTU_QUOTE_QUOTA", 300),
  ];
  await rs.pollOnce();
  assert.equal(relayChanges.length, 1);
  assert.equal(relayChanges[0].eff.IB_REALTIME_MAX_LINES, 40);
  assert.equal(relayChanges[0].prev.IB_REALTIME_MAX_LINES, 85);
  assert.equal(futuConfigs.at(-1).FUTU_QUOTE_QUOTA, 300);

  await rs.pollOnce(); // unchanged → no callbacks
  assert.equal(relayChanges.length, 1);
  assert.equal(futuConfigs.length, 2);

  fetchImpl.fail = true;
  assert.equal(await rs.pollOnce(), false);
  fetchImpl.fail = false;
  fetchImpl.status = 500;
  assert.equal(await rs.pollOnce(), false);
  assert.equal(rs.effective().IB_REALTIME_MAX_LINES, 40); // last values kept
  assert.equal(rs.status().last_error, "http_500");

  fetchImpl.status = 200;
  fetchImpl.rows = []; // override removed → back to env
  await rs.pollOnce();
  assert.equal(relayChanges.at(-1).eff.IB_REALTIME_MAX_LINES, 85);
  assert.equal(futuConfigs.at(-1).FUTU_QUOTE_QUOTA, null);
});

test("poller without an internal token never calls the api", async () => {
  const fetchImpl = fakeFetch();
  const rs = createRuntimeSettings({
    url: "http://api/admin/settings",
    token: "",
    fetchImpl,
  });
  assert.equal(await rs.pollOnce(), false);
  assert.equal(fetchImpl.calls.length, 0);
  assert.equal(rs.status().last_error, "no_internal_token");
});

test("poller failure text never carries the token (native fetch)", async () => {
  // Native fetch echoes an invalid header value in its error message.
  const secret = "SYNTHETIC-TOKEN\nLEAK";
  const logs = [];
  const rs = createRuntimeSettings({
    url: "http://127.0.0.1:9/admin/settings",
    token: secret,
    log: (m) => logs.push(m),
  });
  assert.equal(await rs.pollOnce(), false);
  const seen = logs.join("\n") + JSON.stringify(rs.status());
  for (const part of ["SYNTHETIC-TOKEN", "LEAK"]) assert.ok(!seen.includes(part), seen);
  assert.equal(rs.status().last_error, "request_failed");
});

// ─── relay subprocess (futu_stack harness) ────────────────────────────────

test("relay forwards futu-quotes keys in a config frame, live", async (t) => {
  const stack = await startStack();
  t.after(stack.close);
  stack.settings.rows = [
    row("FUTU_QUOTE_QUOTA", 400),
    row("FUTU_UNITS_PER_SYMBOL", 0),
  ];
  await stack.waitFor(() => stack.configs.length > 0);
  assert.deepEqual(stack.configs.at(-1).values, {
    FUTU_QUOTE_QUOTA: 400,
    FUTU_UNITS_PER_SYMBOL: null, // invalid value ignored → service env
    FUTU_BOOK_STALE_S: null,
    FUTU_GREEK_FACTORS: null,
  });
  assert.equal(stack.settings.requests[0]["x-internal-token"], INTERNAL_TOKEN);
  assert.equal(stack.settings.requests[0]["x-api-key"], undefined);

  stack.settings.rows = [row("FUTU_GREEK_FACTORS", { iv: 0.01 })];
  await stack.waitFor(
    () => stack.configs.at(-1).values.FUTU_GREEK_FACTORS?.iv === 0.01,
  );
  assert.equal(stack.configs.at(-1).values.FUTU_QUOTE_QUOTA, null);
});

test("relay: XENON_QUOTES_DEFAULT override re-routes connected tickets live", async (t) => {
  const stack = await startStack(); // env default ib
  t.after(stack.close);
  const browser = await stack.connect({
    ticket: "rs1",
    origin: "http://localhost:3200",
  });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  await sleep(300);
  assert.ok(!(stack.wants.at(-1)?.symbols ?? []).includes("AAPL"));

  stack.settings.rows = [row("XENON_QUOTES_DEFAULT", "futu-first")];
  await stack.admitOnFutu(["AAPL"]);
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { AAPL: { symbol: "AAPL", last: 12.25 } },
  });
  await stack.waitFutuValue(browser, "AAPL", 12.25, { timeout: 3000 });
  const st = await stack.status();
  assert.equal(
    st.runtime_settings.effective.XENON_QUOTES_DEFAULT,
    "futu-first",
  );

  // api failing: last effective values stay
  stack.settings.status = 500;
  await sleep(300);
  const failing = await stack.status();
  assert.equal(
    failing.runtime_settings.effective.XENON_QUOTES_DEFAULT,
    "futu-first",
  );
  assert.equal(failing.runtime_settings.last_error, "http_500");

  // override removed → back to the env default: AAPL leaves `want`
  stack.settings.status = 200;
  stack.settings.rows = [];
  await stack.waitFor(() => !stack.wants.at(-1).symbols.includes("AAPL"));
  browser.close();
});

test("relay: XENON_FUTU_OPTIONS override unpins / re-pins option keys live", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "futu-first" });
  t.after(stack.close);
  const browser = await stack.connect({
    ticket: "rs2",
    origin: "http://localhost:3200",
  });
  browser.send(JSON.stringify({ action: "subscribe", contracts: [OPTION] }));
  await sleep(300);
  assert.ok(!(stack.wants.at(-1)?.symbols ?? []).includes(OPTION_KEY));

  stack.settings.rows = [row("XENON_FUTU_OPTIONS", true)];
  await stack.waitFor(() => stack.wants.at(-1)?.symbols?.includes(OPTION_KEY));

  stack.settings.rows = [row("XENON_FUTU_OPTIONS", false)];
  await stack.waitFor(() => !stack.wants.at(-1).symbols.includes(OPTION_KEY));
  browser.close();
});

test("relay: line budget and stale threshold follow the override", async (t) => {
  const stack = await startStack();
  t.after(stack.close);
  stack.settings.rows = [
    row("IB_REALTIME_MAX_LINES", 20),
    row("IB_REALTIME_STALE_TICK_MS", 5000),
  ];
  // Awaited polling: no status fetch is left in flight when the test ends.
  let st;
  for (const deadline = Date.now() + 4000; ; await sleep(25)) {
    st = await stack.status();
    if (st?.runtime_settings?.effective?.IB_REALTIME_MAX_LINES === 20) break;
    assert.ok(Date.now() < deadline, "line budget override not applied");
  }
  assert.equal(st.runtime_settings.effective.IB_REALTIME_STALE_TICK_MS, 5000);
  assert.deepEqual(st.runtime_settings.overrides, {
    IB_REALTIME_MAX_LINES: 20,
    IB_REALTIME_STALE_TICK_MS: 5000,
  });
});
