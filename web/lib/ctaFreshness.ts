const ET_TIME_ZONE = "America/New_York";
const MARKET_CLOSE_MINUTES = 16 * 60;

export type CtaStaleReason = "fresh" | "behind_target" | "missing_cache";

export interface CtaCacheMeta {
  last_refresh: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number | null;
  target_date: string;
  expected_date?: string | null;
  latest_cache_date: string | null;
  latest_available_date?: string | null;
  stale_reason: CtaStaleReason;
}

/**
 * Extract ET date/time parts directly using Intl (no double-conversion).
 */
function etParts(now: Date): { year: number; month: number; day: number; hour: number; minute: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ET_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);
  const get = (type: string) => Number(parts.find((p) => p.type === type)!.value);
  return { year: get("year"), month: get("month"), day: get("day"), hour: get("hour"), minute: get("minute") };
}

function formatYMD(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function isTradingWeekday(value: Date): boolean {
  const day = value.getDay();
  return day !== 0 && day !== 6;
}

export function latestClosedTradingDayET(now: Date = new Date()): string {
  const { year, month, day, hour, minute } = etParts(now);
  const minutes = hour * 60 + minute;

  const candidate = new Date(year, month - 1, day);

  if (!(isTradingWeekday(candidate) && minutes >= MARKET_CLOSE_MINUTES)) {
    candidate.setDate(candidate.getDate() - 1);
  }

  while (!isTradingWeekday(candidate)) {
    candidate.setDate(candidate.getDate() - 1);
  }

  return formatYMD(candidate);
}

export function buildCtaCacheMeta(params: {
  targetDate: string;
  latestCacheDate: string | null;
  mtimeMs: number | null;
}): CtaCacheMeta {
  const { targetDate, latestCacheDate, mtimeMs } = params;
  const ageSeconds = typeof mtimeMs === "number" ? Math.max(0, Math.floor((Date.now() - mtimeMs) / 1000)) : null;
  const isStale = !latestCacheDate || latestCacheDate !== targetDate;
  const staleReason: CtaStaleReason = !latestCacheDate
    ? "missing_cache"
    : latestCacheDate === targetDate
      ? "fresh"
      : "behind_target";

  return {
    last_refresh: typeof mtimeMs === "number" ? new Date(mtimeMs).toISOString() : null,
    age_seconds: ageSeconds,
    is_stale: isStale,
    stale_threshold_seconds: null,
    target_date: targetDate,
    latest_cache_date: latestCacheDate,
    stale_reason: staleReason,
  };
}
