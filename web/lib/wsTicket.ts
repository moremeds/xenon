"use client";

/**
 * Obtain a short-lived WebSocket ticket from the API.
 * Called from browser before establishing WebSocket connections.
 *
 * Routes through Next.js API (/api/ib/ws-ticket) which proxies to FastAPI
 * server-to-server with the internal token. This avoids cross-origin issues
 * in local dev (browser on :3200, FastAPI on :8421) and works behind the
 * tailnet proxy in production. The mutating-route Content-Type is required by
 * the Next middleware origin guard.
 */

export async function getWsTicket(): Promise<string> {
  const res = await fetch("/api/ib/ws-ticket", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
  });

  if (!res.ok) {
    throw new Error(`Failed to obtain WS ticket: ${res.status}`);
  }

  const data = await res.json();
  if (typeof data.ticket !== "string" || data.ticket.length === 0) {
    throw new Error("WS ticket response missing ticket");
  }
  return data.ticket;
}
