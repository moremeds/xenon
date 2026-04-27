import { NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { join } from "path";
import { xenonFetch } from "@/lib/xenonApi";
import {
  getRequestId,
  jsonApiError,
  setNoStoreResponseHeaders,
} from "@/lib/apiContracts";

export const runtime = "nodejs";

// Phase 1 of the portfolio postgres read-path migration: this route used to
// read data/portfolio.json directly, which went stale after PR #52 moved the
// writer into Postgres. Both GET and POST now hit the new FastAPI
// `GET /portfolio` endpoint, which queries account_snapshots.payload scoped
// by the current AccountScope. See
// docs/plans/2026-04-27-portfolio-postgres-read-path.md.

const TRADE_LOG_PATH = join(process.cwd(), "..", "data", "trade_log.json");
const STALE_AFTER_MS = 60_000;

/** Load ticker → earliest trade date from trade_log.json. Read straight from
 * the file because trade_log.json is still owned by the prior writer; Phase 2
 * will migrate it to PG along with the other 8 readers. */
async function loadTradeLogDates(): Promise<Record<string, string>> {
  try {
    const raw = JSON.parse(await readFile(TRADE_LOG_PATH, "utf-8"));
    const trades = Array.isArray(raw) ? raw : (raw?.trades ?? []);
    const dates: Record<string, string> = {};
    for (const t of trades) {
      const ticker = t?.ticker;
      const date = t?.date;
      if (typeof ticker === "string" && typeof date === "string") {
        if (!dates[ticker] || date > dates[ticker]) {
          dates[ticker] = date;
        }
      }
    }
    return dates;
  } catch {
    return {};
  }
}

let bgSyncInFlight = false;

/** Fire-and-forget: call FastAPI background sync endpoint */
function triggerBackgroundSync(): void {
  if (bgSyncInFlight) return;
  bgSyncInFlight = true;

  console.log("[Portfolio] Background sync triggered via FastAPI");
  xenonFetch("/portfolio/background-sync", { method: "POST", timeout: 5_000 })
    .then(() => {
      console.log("[Portfolio] Background sync accepted");
    })
    .catch((err) => {
      console.warn("[Portfolio] Background sync trigger failed:", err.message);
    })
    .finally(() => {
      bgSyncInFlight = false;
    });
}

/** Parse the FastAPI payload's `last_sync` and decide if it's stale. */
function isResponseStale(payload: { last_sync?: unknown }): boolean {
  const raw = typeof payload.last_sync === "string" ? payload.last_sync : null;
  if (!raw) return true;
  const ts = Date.parse(raw);
  if (Number.isNaN(ts)) return true;
  return Date.now() - ts > STALE_AFTER_MS;
}

export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    const data = (await xenonFetch("/portfolio", {
      method: "GET",
      timeout: 10_000,
    })) as Record<string, unknown>;
    if (isResponseStale(data)) {
      triggerBackgroundSync();
    }
    const tradeLogDates = await loadTradeLogDates();
    const response = NextResponse.json({
      ...data,
      trade_log_dates: tradeLogDates,
    });
    return setNoStoreResponseHeaders(response, requestId);
  } catch (error) {
    const status = (error as { status?: number })?.status ?? 502;
    // 404 → no snapshot yet; trigger a sync and tell the client to retry.
    if (status === 404) {
      triggerBackgroundSync();
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "No portfolio snapshot yet — sync triggered, retry shortly.",
          status: 404,
          code: "NOT_FOUND",
          requestId,
        }),
        requestId,
      );
    }
    const message =
      error instanceof Error ? error.message : "Failed to read portfolio";
    return setNoStoreResponseHeaders(
      jsonApiError({
        message,
        status,
        code: "UPSTREAM_ERROR",
        requestId,
      }),
      requestId,
    );
  }
}

export async function POST(): Promise<Response> {
  const requestId = getRequestId();
  try {
    const data = (await xenonFetch("/portfolio/sync", {
      method: "POST",
      timeout: 35_000,
    })) as Record<string, unknown>;
    const tradeLogDates = await loadTradeLogDates();
    const response = NextResponse.json({
      ...data,
      trade_log_dates: tradeLogDates,
    });
    return setNoStoreResponseHeaders(response, requestId);
  } catch (error) {
    const status = (error as { status?: number })?.status ?? 502;
    const message = error instanceof Error ? error.message : "Sync failed";
    return setNoStoreResponseHeaders(
      jsonApiError({
        message,
        status,
        code: "UPSTREAM_ERROR",
        requestId,
      }),
      requestId,
    );
  }
}
