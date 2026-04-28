import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { getRequestId, setNoStoreResponseHeaders } from "@/lib/apiContracts";

export const runtime = "nodejs";

type PerformancePayload = Record<string, unknown>;

type PerformanceCache = {
  data: PerformancePayload;
  fetchedAtMs: number;
};

const OPEN_TTL_ENV = "XENON_PERFORMANCE_TTL_OPEN_S";
const CLOSED_TTL_ENV = "XENON_PERFORMANCE_TTL_CLOSED_S";
const DEFAULT_OPEN_TTL_SECONDS = 5 * 60;
const DEFAULT_CLOSED_TTL_SECONDS = 30 * 60;

let performanceCache: PerformanceCache | null = null;
let refreshInFlight: Promise<PerformancePayload> | null = null;

function ttlSecondsFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function isMarketOpenNow(now: Date = new Date()): boolean {
  const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const day = et.getDay();
  if (day === 0 || day === 6) return false;
  const minutes = et.getHours() * 60 + et.getMinutes();
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60;
}

function cacheTtlMs(now: Date = new Date()): number {
  const ttlSeconds = isMarketOpenNow(now)
    ? ttlSecondsFromEnv(OPEN_TTL_ENV, DEFAULT_OPEN_TTL_SECONDS)
    : ttlSecondsFromEnv(CLOSED_TTL_ENV, DEFAULT_CLOSED_TTL_SECONDS);
  return ttlSeconds * 1_000;
}

function isCacheFresh(cache: PerformanceCache, nowMs: number = Date.now()): boolean {
  return nowMs - cache.fetchedAtMs <= cacheTtlMs(new Date(nowMs));
}

async function fetchPerformance(timeout: number): Promise<PerformancePayload> {
  const data = await xenonFetch<PerformancePayload>("/performance", {
    method: "POST",
    timeout,
  });
  performanceCache = {
    data,
    fetchedAtMs: Date.now(),
  };
  return data;
}

function refreshPerformanceCache(timeout: number): Promise<PerformancePayload> {
  if (!refreshInFlight) {
    refreshInFlight = fetchPerformance(timeout).finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

function triggerBackgroundRefresh(): void {
  refreshPerformanceCache(180_000).catch(() => {});
}

function jsonWithNoStore(data: PerformancePayload, requestId: string): NextResponse {
  return setNoStoreResponseHeaders(NextResponse.json(data), requestId);
}

export async function GET(): Promise<Response> {
  const requestId = getRequestId();

  if (performanceCache && isCacheFresh(performanceCache)) {
    return jsonWithNoStore(performanceCache.data, requestId);
  }

  if (performanceCache) {
    triggerBackgroundRefresh();
    return jsonWithNoStore(performanceCache.data, requestId);
  }

  try {
    const data = await refreshPerformanceCache(180_000);
    return jsonWithNoStore(data, requestId);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to generate performance metrics";
    return setNoStoreResponseHeaders(
      NextResponse.json({ error: message }, { status: 502 }),
      requestId,
    );
  }
}

export async function POST(): Promise<Response> {
  const requestId = getRequestId();
  try {
    const data = await refreshPerformanceCache(190_000);
    return jsonWithNoStore(data, requestId);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to generate performance metrics";
    return setNoStoreResponseHeaders(
      NextResponse.json({ error: message }, { status: 502 }),
      requestId,
    );
  }
}
