import { NextResponse } from "next/server";
import { isCriDataStale } from "@/lib/criStaleness";
import { backfillRealizedVolHistory, type RegimeHistoryEntry } from "@/lib/regimeHistory";
import { xenonFetch } from "@/lib/xenonApi";
import { getRequestId, setCacheResponseHeaders } from "@/lib/apiContracts";

export const runtime = "nodejs";

/** Today's date in ET (YYYY-MM-DD) — the trading calendar reference */
function todayET(): string {
  return new Date().toLocaleDateString("sv", { timeZone: "America/New_York" });
}

/** Real-time market open check: Mon-Fri, 9:30-16:00 ET */
function isMarketOpenNow(): boolean {
  const now = new Date();
  const etStr = now.toLocaleString("en-US", { timeZone: "America/New_York" });
  const et = new Date(etStr);
  const day = et.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) return false;
  const minutes = et.getHours() * 60 + et.getMinutes();
  return minutes >= 9 * 60 + 30 && minutes <= 16 * 60;
}

const EMPTY_CRI = {
  scan_time: "",
  date: "",
  vix: null,
  vvix: null,
  spy: null,
  vix_5d_roc: null,
  vvix_vix_ratio: null,
  spx_100d_ma: null,
  spx_distance_pct: null,
  cor1m: null,
  cor1m_previous_close: null,
  cor1m_5d_change: null,
  realized_vol: null,
  cri: { score: 0, level: "LOW", components: { vix: 0, vvix: 0, correlation: 0, momentum: 0 } },
  cta: { realized_vol: 0, exposure_pct: 200, forced_reduction_pct: 0, est_selling_bn: 0 },
  menthorq_cta: null,
  crash_trigger: { triggered: false, conditions: { spx_below_100d_ma: false, realized_vol_gt_25: false, cor1m_gt_60: false }, values: {} },
  history: [],
  spy_closes: [],
};

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeCriComponents(raw: Record<string, unknown> | undefined) {
  const components = raw ?? {};
  return {
    vix: asNumber(components.vix) ?? EMPTY_CRI.cri.components.vix,
    vvix: asNumber(components.vvix) ?? EMPTY_CRI.cri.components.vvix,
    correlation: asNumber(components.correlation) ?? EMPTY_CRI.cri.components.correlation,
    momentum: asNumber(components.momentum) ?? EMPTY_CRI.cri.components.momentum,
  };
}

function normalizeCriPayload(raw: Record<string, unknown>): Record<string, unknown> {
  const crashTrigger = (raw.crash_trigger as Record<string, unknown>) ?? {};
  const conditions = (crashTrigger.conditions as Record<string, unknown>) ?? {};
  const spyCloses = Array.isArray(raw.spy_closes)
    ? raw.spy_closes
      .map((value) => asNumber(value))
      .filter((value): value is number => value !== null)
    : [];
  const history = Array.isArray(raw.history)
    ? backfillRealizedVolHistory(raw.history as RegimeHistoryEntry[], spyCloses)
    : [];
  const latestHistoryCor1m = history.length > 0
    ? asNumber(history[history.length - 1].cor1m)
    : null;
  const latestRealizedVol = history.length > 0 ? asNumber(history[history.length - 1].realized_vol) : null;
  const normalizedRealizedVol = asNumber(raw.realized_vol) ?? latestRealizedVol;

  const rawCri = (raw.cri as Record<string, unknown>) ?? {};
  const rawCriLevel = asString(rawCri.level);
  const normalizedCriLevel = ["LOW", "ELEVATED", "HIGH", "CRITICAL"].includes(rawCriLevel)
    ? rawCriLevel
    : EMPTY_CRI.cri.level;
  const rawCta = (raw.cta as Record<string, unknown>) ?? {};

  return {
    ...EMPTY_CRI,
    scan_time: asString(raw.scan_time),
    date: asString(raw.date),
    market_open: asBoolean(raw.market_open),
    vix: asNumber(raw.vix),
    vvix: asNumber(raw.vvix),
    spy: asNumber(raw.spy),
    vix_5d_roc: asNumber(raw.vix_5d_roc),
    vvix_vix_ratio: asNumber(raw.vvix_vix_ratio),
    spx_100d_ma: asNumber(raw.spx_100d_ma),
    spx_distance_pct: asNumber(raw.spx_distance_pct),
    cor1m: asNumber(raw.cor1m),
    cor1m_previous_close:
      asNumber(raw.cor1m_previous_close) ?? latestHistoryCor1m ?? EMPTY_CRI.cor1m_previous_close,
    cor1m_5d_change: asNumber(raw.cor1m_5d_change),
    realized_vol: normalizedRealizedVol,
    cri: {
      ...EMPTY_CRI.cri,
      ...rawCri,
      score: asNumber(rawCri.score) ?? EMPTY_CRI.cri.score,
      level: normalizedCriLevel,
      components: {
        ...EMPTY_CRI.cri.components,
        ...normalizeCriComponents(rawCri.components as Record<string, unknown>),
      },
    },
    cta: {
      ...EMPTY_CRI.cta,
      ...rawCta,
      realized_vol: asNumber(rawCta.realized_vol) ?? EMPTY_CRI.cta.realized_vol,
      exposure_pct: asNumber(rawCta.exposure_pct) ?? EMPTY_CRI.cta.exposure_pct,
      forced_reduction_pct:
        asNumber(rawCta.forced_reduction_pct) ?? EMPTY_CRI.cta.forced_reduction_pct,
      est_selling_bn: asNumber(rawCta.est_selling_bn) ?? EMPTY_CRI.cta.est_selling_bn,
    },
    menthorq_cta: raw.menthorq_cta ?? null,
    history,
    spy_closes: spyCloses,
    crash_trigger: {
      ...EMPTY_CRI.crash_trigger,
      ...crashTrigger,
      triggered:
        typeof crashTrigger.triggered === "boolean" ? crashTrigger.triggered : EMPTY_CRI.crash_trigger.triggered,
      conditions: {
        ...EMPTY_CRI.crash_trigger.conditions,
        ...conditions,
        spx_below_100d_ma:
          typeof conditions.spx_below_100d_ma === "boolean"
            ? conditions.spx_below_100d_ma
            : EMPTY_CRI.crash_trigger.conditions.spx_below_100d_ma,
        realized_vol_gt_25:
          typeof conditions.realized_vol_gt_25 === "boolean"
            ? conditions.realized_vol_gt_25
            : EMPTY_CRI.crash_trigger.conditions.realized_vol_gt_25,
        cor1m_gt_60: typeof conditions.cor1m_gt_60 === "boolean" ? conditions.cor1m_gt_60 : false,
      },
      values:
        typeof crashTrigger.values === "object" && crashTrigger.values !== null
          ? crashTrigger.values
          : {},
    },
  };
}

function payloadMtimeMs(data: Record<string, unknown>): number {
  const raw =
    (typeof data._scanned_at === "string" && data._scanned_at) ||
    (typeof data.scan_time === "string" && data.scan_time) ||
    null;
  const parsed = raw ? Date.parse(raw) : NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

let bgScanInFlight = false;

/** Fire-and-forget: run CRI scan via FastAPI and save results */
function triggerBackgroundScan(): void {
  if (bgScanInFlight) return;
  bgScanInFlight = true;

  console.log("[CRI] Background scan triggered via FastAPI");
  xenonFetch<Record<string, unknown>>("/regime/scan", { method: "POST", timeout: 130_000 })
    .then(() => { console.log("[CRI] Background scan complete"); })
    .catch((err) => { console.error("[CRI] Background scan failed:", err.message); })
    .finally(() => { bgScanInFlight = false; });
}

export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  let upstream: Record<string, unknown> | null = null;
  try {
    upstream = await xenonFetch<Record<string, unknown>>("/regime", {
      method: "GET",
      timeout: 10_000,
    });
  } catch (err) {
    console.warn("[CRI] FastAPI /regime failed:", err);
  }
  const data = normalizeCriPayload((upstream ?? EMPTY_CRI) as Record<string, unknown>);
  const currentMarketOpen = isMarketOpenNow();

  // Keep market_open aligned with the current session state for every request.
  (data as Record<string, unknown>).market_open = currentMarketOpen;

  // Stale-while-revalidate: return latest PG data immediately, kick off a
  // background scan if today's data is stale or absent.
  if (!upstream || isCriDataStale(data, payloadMtimeMs(upstream), todayET(), currentMarketOpen)) {
    triggerBackgroundScan();
  }

  const response = NextResponse.json(data);
  return setCacheResponseHeaders(response, {
    maxAgeSeconds: 15,
    staleWhileRevalidateSeconds: 120,
    requestId,
    cacheState: "HIT",
    tags: ["regime"],
  });
}

export async function POST(): Promise<Response> {
  try {
    const rawData = await xenonFetch<Record<string, unknown>>("/regime/scan", {
      method: "POST",
      timeout: 130_000,
    });
    const data = normalizeCriPayload(rawData);
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "CRI scan failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
