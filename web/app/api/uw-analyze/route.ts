import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import type { UwAnalyzeResponse } from "@/lib/types/uwAnalyze";

export const runtime = "nodejs";

export async function POST(req: Request): Promise<Response> {
  try {
    const { getToken } = await auth();
    const token = (await getToken()) ?? undefined;
    const body = await req.json().catch(() => ({}));
    const ticker = typeof body?.ticker === "string" ? body.ticker.trim() : "";
    if (!ticker) {
      return NextResponse.json({ error: "ticker required" }, { status: 400 });
    }
    const data = await xenonFetch<UwAnalyzeResponse>("/uw-analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
      token,
      timeout: 90_000,
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof XenonApiError) {
      return NextResponse.json({ error: err.detail }, { status: err.status });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Unknown error" },
      { status: 500 },
    );
  }
}
