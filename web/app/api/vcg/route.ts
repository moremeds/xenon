import { NextResponse } from "next/server";
import { isVcgDataStale } from "@/lib/vcgStaleness";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import { getRequestId, setCacheResponseHeaders } from "@/lib/apiContracts";

export const runtime = "nodejs";

const EMPTY_VCG = {
  scan_time: "",
  market_open: false,
  credit_proxy: "HYG",
  signal: {
    vcg: null,
    vcg_adj: null,
    residual: null,
    beta1_vvix: null,
    beta2_vix: null,
    alpha: null,
    vix: 0,
    vvix: 0,
    credit_price: 0,
    credit_5d_return_pct: 0,
    ro: 0,
    edr: 0,
    tier: null,
    bounce: 0,
    vvix_severity: "moderate",
    sign_ok: true,
    sign_suppressed: false,
    pi_panic: 0,
    regime: "DIVERGENCE",
    interpretation: "NORMAL",
    attribution: {
      vvix_pct: 0,
      vix_pct: 0,
      vvix_component: 0,
      vix_component: 0,
      model_implied: 0,
    },
  },
  history: [],
};

function isMarketOpenNow(): boolean {
  const now = new Date();
  const et = new Date(
    now.toLocaleString("en-US", { timeZone: "America/New_York" }),
  );
  const day = et.getDay();
  if (day === 0 || day === 6) return false;
  const minutes = et.getHours() * 60 + et.getMinutes();
  return minutes >= 9 * 60 + 30 && minutes <= 16 * 60;
}

function todayET(): string {
  return new Date().toLocaleDateString("sv", { timeZone: "America/New_York" });
}

function normalizeVcgPayload(
  raw: Record<string, unknown>,
): Record<string, unknown> {
  const signal = (raw.signal as Record<string, unknown>) ?? {};
  const attr = (signal.attribution as Record<string, unknown>) ?? {};

  return {
    ...EMPTY_VCG,
    scan_time: typeof raw.scan_time === "string" ? raw.scan_time : "",
    market_open:
      typeof raw.market_open === "boolean"
        ? raw.market_open
        : isMarketOpenNow(),
    credit_proxy:
      typeof raw.credit_proxy === "string" ? raw.credit_proxy : "HYG",
    signal: {
      ...EMPTY_VCG.signal,
      ...signal,
      attribution: { ...EMPTY_VCG.signal.attribution, ...attr },
    },
    history: Array.isArray(raw.history) ? raw.history : [],
  };
}

let bgScanInFlight = false;

function triggerBackgroundScan(): void {
  if (bgScanInFlight) return;
  bgScanInFlight = true;

  console.log("[VCG] Background scan triggered via FastAPI");
  xenonFetch<Record<string, unknown>>("/vcg/scan", {
    method: "POST",
    timeout: 130_000,
  })
    .then(() => {
      console.log("[VCG] Background scan complete");
    })
    .catch((err) => {
      console.error("[VCG] Background scan failed:", err.message);
    })
    .finally(() => {
      bgScanInFlight = false;
    });
}

async function fetchLatestVcg(): Promise<Record<string, unknown> | null> {
  try {
    return await xenonFetch<Record<string, unknown>>("/vcg", {
      method: "GET",
      timeout: 5_000,
    });
  } catch (err) {
    if (err instanceof XenonApiError) {
      console.warn(`[VCG] FastAPI /vcg failed: ${err.message}`);
    } else {
      console.warn(`[VCG] FastAPI /vcg failed:`, err);
    }
    return null;
  }
}

export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  const upstream = await fetchLatestVcg();
  const data = normalizeVcgPayload(upstream ?? {});
  const currentMarketOpen = isMarketOpenNow();
  (data as Record<string, unknown>).market_open = currentMarketOpen;

  // Stale-while-revalidate: empty payload (no scan rows yet) or stale by date.
  const stale = upstream
    ? isVcgDataStale(
        upstream as { scan_time?: string; market_open?: boolean },
        todayET(),
        currentMarketOpen,
      )
    : true;

  if (stale) {
    triggerBackgroundScan();
  }

  const response = NextResponse.json(data);
  return setCacheResponseHeaders(response, {
    maxAgeSeconds: 15,
    staleWhileRevalidateSeconds: 120,
    requestId,
    cacheState: "HIT",
    tags: ["vcg"],
  });
}
