import { test } from "node:test";
import assert from "node:assert/strict";
import {
  authorizeUpgrade,
  isOriginAllowed,
  allowedOrigins,
} from "../ws_upgrade_auth.js";

// Relay upgrade authorization: every upgrade needs a valid single-use ticket
// (validated by FastAPI — expiry and single-use live there) and browser
// origins must be on the allowlist. No loopback bypass.

const ORIGINS = allowedOrigins();

function makeReq({ url = "/", host = "localhost:8765", origin, apiKey } = {}) {
  const headers = { host };
  if (origin !== undefined) headers.origin = origin;
  if (apiKey !== undefined) headers["x-api-key"] = apiKey;
  return { url, headers };
}

const okFetch = async () => ({ ok: true });
const failFetch = async () => ({ ok: false, status: 401 });
const DEPS = { fetchImpl: okFetch, origins: ORIGINS };

test("no ticket -> rejected (even from loopback host)", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/" }), DEPS);
  assert.equal(d.ok, false);
  assert.equal(d.status, 401);
  assert.equal(d.reason, "missing_ticket");
});

test("expired/invalid ticket -> rejected when validate returns non-ok", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/?ticket=expired" }), {
    fetchImpl: failFetch,
    origins: ORIGINS,
  });
  assert.equal(d.ok, false);
  assert.equal(d.status, 401);
  assert.equal(d.reason, "ticket_rejected");
});

test("replayed ticket -> validate rejects it (single-use enforced upstream)", async () => {
  // First use succeeds, replay fails because FastAPI consumed the ticket.
  let calls = 0;
  const replayFetch = async () => ({ ok: calls++ === 0 });
  const first = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), {
    fetchImpl: replayFetch,
    origins: ORIGINS,
  });
  const second = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), {
    fetchImpl: replayFetch,
    origins: ORIGINS,
  });
  assert.equal(first.ok, true);
  assert.equal(second.ok, false);
  assert.equal(second.status, 401);
});

test("foreign Origin -> 403 before ticket validation", async () => {
  let fetchCalled = false;
  const d = await authorizeUpgrade(
    makeReq({
      url: "/?ticket=t1",
      origin: "https://evil.example",
    }),
    {
      fetchImpl: async () => {
        fetchCalled = true;
        return { ok: true };
      },
      origins: ORIGINS,
    },
  );
  assert.equal(d.ok, false);
  assert.equal(d.status, 403);
  assert.equal(d.reason, "origin_not_allowed");
  assert.equal(fetchCalled, false);
});

test("allowed browser Origin + valid ticket -> accepted", async () => {
  const d = await authorizeUpgrade(
    makeReq({ url: "/?ticket=t1", origin: "http://localhost:3000" }),
    DEPS,
  );
  assert.deepEqual(d, { ok: true });
});

test("absent Origin (server-side ws client) + valid ticket -> accepted", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), DEPS);
  assert.equal(d.ok, true);
});

test("validate endpoint unreachable -> 503, no upgrade", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), {
    fetchImpl: async () => {
      throw new Error("ECONNREFUSED");
    },
    origins: ORIGINS,
  });
  assert.equal(d.ok, false);
  assert.equal(d.status, 503);
});

// ── Service credential path (X-API-Key, no Origin) ──────────────────────

test("correct X-API-Key with no Origin -> accepted without ticket", async () => {
  let fetchCalled = false;
  const d = await authorizeUpgrade(makeReq({ url: "/", apiKey: "svc-key-1" }), {
    fetchImpl: async () => {
      fetchCalled = true;
      return { ok: true };
    },
    origins: ORIGINS,
    queryApiKey: "svc-key-1",
  });
  assert.deepEqual(d, { ok: true });
  assert.equal(fetchCalled, false); // no ticket validation call made
});

test("wrong X-API-Key -> 401", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/", apiKey: "wrong" }), {
    fetchImpl: okFetch,
    origins: ORIGINS,
    queryApiKey: "svc-key-1",
  });
  assert.equal(d.ok, false);
  assert.equal(d.status, 401);
  assert.equal(d.reason, "bad_api_key");
});

test("correct X-API-Key WITH an Origin -> rejected", async () => {
  const d = await authorizeUpgrade(
    makeReq({ url: "/", apiKey: "svc-key-1", origin: "http://localhost:3000" }),
    {
      fetchImpl: okFetch,
      origins: ORIGINS,
      queryApiKey: "svc-key-1",
    },
  );
  assert.equal(d.ok, false);
  assert.equal(d.status, 403);
});

test("XENON_QUERY_API_KEY unset -> key path disabled", async () => {
  const d = await authorizeUpgrade(makeReq({ url: "/", apiKey: "svc-key-1" }), {
    fetchImpl: okFetch,
    origins: ORIGINS,
    queryApiKey: "",
  });
  assert.equal(d.ok, false);
  assert.equal(d.status, 401);
  assert.equal(d.reason, "api_key_disabled");
});

// ── Ticket validation authenticates itself to FastAPI ───────────────────

test("validate call carries X-Internal-Token when configured", async () => {
  let seenHeaders;
  const d = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), {
    fetchImpl: async (_url, init) => {
      seenHeaders = init.headers;
      return { ok: true };
    },
    origins: ORIGINS,
    internalToken: "internal-tok-1",
  });
  assert.equal(d.ok, true);
  assert.equal(seenHeaders["X-Internal-Token"], "internal-tok-1");
});

test("validate call omits X-Internal-Token when unset (server 401s)", async () => {
  let seenHeaders;
  const d = await authorizeUpgrade(makeReq({ url: "/?ticket=t1" }), {
    fetchImpl: async (_url, init) => {
      seenHeaders = init.headers;
      return { ok: false, status: 401 }; // FastAPI rejects the credentialless call
    },
    origins: ORIGINS,
    internalToken: "",
  });
  assert.equal(seenHeaders["X-Internal-Token"], undefined);
  assert.equal(d.ok, false);
  assert.equal(d.status, 401);
  assert.equal(d.reason, "ticket_rejected");
});

test("isOriginAllowed honors the env-configured allowlist", () => {
  const origins = allowedOrigins("https://xenon.tailnet.ts.net");
  assert.equal(isOriginAllowed("https://xenon.tailnet.ts.net", origins), true);
  assert.equal(isOriginAllowed("https://other.example", origins), false);
  assert.equal(isOriginAllowed(undefined, origins), true);
});
