import { NextResponse } from "next/server";
import { XenonApiError, xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

/** One raw Unusual Whales `screener/analysts` row (FastAPI passes it through). */
type UwAnalystRow = {
  timestamp?: string | null;
  firm?: string | null;
  analyst_name?: string | null;
  action?: string | null;
  recommendation?: string | null;
  target?: string | number | null;
};

type FastApiRatings = {
  ticker: string;
  source: string;
  as_of: string;
  ratings: UwAnalystRow[];
};

function toTarget(value: UwAnalystRow["target"]): number | null {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

const TIMEOUT_BODY = {
  error: "Ratings request timed out",
  reason_code: "RATINGS_TIMEOUT",
} as const;

function isAbort(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

/**
 * Stable, generic error bodies with a top-level reason_code. Raw exception
 * text (connection errors, upstream detail) is never returned to the browser.
 */
function failure(error: unknown): Response {
  if (isAbort(error)) {
    return NextResponse.json(TIMEOUT_BODY, { status: 504 });
  }
  if (error instanceof XenonApiError) {
    const code = error.body?.reason_code;
    if (error.status === 400 && code === "INVALID_TICKER") {
      return NextResponse.json(
        { error: "Invalid ticker", reason_code: "INVALID_TICKER" },
        { status: 400 },
      );
    }
    if (error.status === 504 || code === "UW_TIMEOUT") {
      return NextResponse.json(TIMEOUT_BODY, { status: 504 });
    }
    return NextResponse.json(
      {
        error: "Ratings unavailable",
        reason_code: code === "UW_UPSTREAM_ERROR" ? code : "RATINGS_UNAVAILABLE",
      },
      { status: 502 },
    );
  }
  return NextResponse.json(
    { error: "Ratings unavailable", reason_code: "RATINGS_UNAVAILABLE" },
    { status: 502 },
  );
}

export async function GET(request: Request): Promise<Response> {
  const { searchParams } = new URL(request.url);
  const ticker = searchParams.get("ticker")?.trim().toUpperCase();

  if (!ticker) {
    return NextResponse.json(
      { error: "ticker parameter required", reason_code: "INVALID_TICKER" },
      { status: 400 },
    );
  }

  try {
    const data = await xenonFetch<FastApiRatings>(
      `/ticker/ratings?ticker=${encodeURIComponent(ticker)}`,
      { timeout: 20_000 },
    );

    // Map raw UW rows onto the RatingsTab "Recent Analyst Actions" shape.
    // No aggregation or scoring: one entry per UW row, fields renamed only.
    return NextResponse.json({
      ticker: data.ticker,
      source: data.source,
      as_of: data.as_of,
      recent_changes: data.ratings.map((r) => ({
        date: r.timestamp ? r.timestamp.slice(0, 10) : undefined,
        firm: r.firm ?? r.analyst_name ?? "---",
        action: r.action ?? "",
        to_grade: r.recommendation ?? undefined,
        target: toTarget(r.target),
      })),
    });
  } catch (error) {
    return failure(error);
  }
}
