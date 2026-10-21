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

export async function GET(request: Request): Promise<Response> {
  const { searchParams } = new URL(request.url);
  const ticker = searchParams.get("ticker")?.trim().toUpperCase();

  if (!ticker) {
    return NextResponse.json(
      { error: "ticker parameter required" },
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
    if (error instanceof XenonApiError) {
      const reasonCode = error.body?.reason_code;
      return NextResponse.json(
        {
          error: error.detail || "Failed to fetch ratings",
          ...(reasonCode ? { reason_code: reasonCode } : {}),
        },
        { status: error.status === 400 ? 400 : 502 },
      );
    }
    const message =
      error instanceof Error ? error.message : "Failed to fetch ratings";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
