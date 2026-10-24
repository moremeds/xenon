// WebSocket upgrade authorization for the IB realtime relay.
//
// Every upgrade must carry a valid single-use ticket (?ticket=...) minted by
// FastAPI /ws-ticket via the Next server. Validation is delegated to
// TICKET_VALIDATE_URL (FastAPI /ws-ticket/validate), which enforces expiry and
// single-use semantics — a replayed ticket fails validation there.
//
// Origin: browsers send Origin on WS upgrades. Only page origins on the
// allowed list are accepted (XENON_WS_ALLOWED_ORIGINS, comma-separated; the
// default covers the local Next dev/prod ports). Non-browser clients (the
// Next `ws` client in previous-close) send no Origin and are allowed.
//
// Service credential: non-browser consumers (argon-ws-consumer,
// apex-deploy-api) can't mint Next tickets. An upgrade carrying X-API-Key
// matching XENON_QUERY_API_KEY is accepted without a ticket — but only with
// no Origin header, since browsers cannot set custom WS headers and service
// clients send none. Env unset → the path is disabled.
//
// No loopback bypass: reachability of 127.0.0.1 is not authentication —
// Tailnet peers, Docker bridges, and proxies can all reach it.

import { timingSafeEqual } from "node:crypto";

const DEFAULT_TICKET_VALIDATE_URL =
  "http://127.0.0.1:8321/ws-ticket/validate";

const DEFAULT_ALLOWED_ORIGINS =
  "http://localhost:3000,http://127.0.0.1:3000," +
  "http://localhost:3200,http://127.0.0.1:3200";

export function ticketValidateUrl(
  envValue = process.env.TICKET_VALIDATE_URL,
) {
  return envValue && envValue.trim() ? envValue : DEFAULT_TICKET_VALIDATE_URL;
}

export function allowedOrigins(
  envValue = process.env.XENON_WS_ALLOWED_ORIGINS,
) {
  const raw = envValue && envValue.trim() ? envValue : DEFAULT_ALLOWED_ORIGINS;
  return new Set(
    raw
      .split(",")
      .map((o) => o.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function isOriginAllowed(originHeader, origins = allowedOrigins()) {
  if (originHeader == null || originHeader === "") return true;
  return origins.has(String(originHeader).trim().toLowerCase());
}

function serviceKeyMatches(presented, expected) {
  const a = Buffer.from(String(presented));
  const b = Buffer.from(String(expected));
  return a.length === b.length && timingSafeEqual(a, b);
}

/**
 * Decide whether an upgrade request may proceed.
 *
 * @param {import("http").IncomingMessage} req
 * @param {{ fetchImpl?: typeof fetch, validateUrl?: string,
 *           origins?: Set<string>, queryApiKey?: string,
 *           internalToken?: string }} [deps]
 * @returns {Promise<{ok:true, auth:"api-key"|"ticket"} |
 *                   {ok:false, status:number, reason:string}>}
 */
export async function authorizeUpgrade(req, deps = {}) {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const validateUrl = deps.validateUrl ?? ticketValidateUrl();
  const origins = deps.origins ?? allowedOrigins();
  const queryApiKey = deps.queryApiKey ?? process.env.XENON_QUERY_API_KEY;
  const internalToken =
    deps.internalToken ?? process.env.XENON_INTERNAL_API_TOKEN;

  if (!isOriginAllowed(req.headers.origin, origins)) {
    return { ok: false, status: 403, reason: "origin_not_allowed" };
  }

  // Service credential path — X-API-Key accepted only when the Origin header
  // is ABSENT entirely (a present-but-empty Origin does not qualify; any
  // present Origin takes the ticket path below). Browsers cannot set custom
  // WS headers, so a browser-shaped request never reaches this branch.
  const apiKeyHeader = req.headers["x-api-key"];
  if (apiKeyHeader != null && req.headers.origin == null) {
    if (!queryApiKey) {
      return { ok: false, status: 401, reason: "api_key_disabled" };
    }
    if (!serviceKeyMatches(apiKeyHeader, queryApiKey)) {
      return { ok: false, status: 401, reason: "bad_api_key" };
    }
    return { ok: true, auth: "api-key" };
  }

  let ticket = null;
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host}`);
    ticket = url.searchParams.get("ticket");
  } catch {
    return { ok: false, status: 401, reason: "bad_url" };
  }
  if (!ticket) {
    return { ok: false, status: 401, reason: "missing_ticket" };
  }

  let res;
  try {
    // /ws-ticket/validate is not auth-exempt — the relay authenticates as the
    // internal service. Missing token -> FastAPI 401 -> ticket_rejected below.
    const headers = { "Content-Type": "application/json" };
    if (internalToken) headers["X-Internal-Token"] = internalToken;
    res = await fetchImpl(validateUrl, {
      method: "POST",
      headers,
      body: JSON.stringify({ ticket }),
    });
  } catch (err) {
    return {
      ok: false,
      status: 503,
      reason:
        "validate_unreachable:" +
        (err instanceof Error ? err.message : String(err)),
    };
  }

  if (!res.ok) {
    return { ok: false, status: 401, reason: "ticket_rejected" };
  }
  return { ok: true, auth: "ticket" };
}
