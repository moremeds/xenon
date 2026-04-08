import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function POST(req: Request): Promise<Response> {
  try {
    const { getToken } = await auth();
    const token = (await getToken()) ?? undefined;
    const body = (await req.json().catch(() => ({}))) as {
      tickers?: string[];
      adhoc?: boolean;
    };
    const data = await xenonFetch<{
      refreshed: number;
      failed: { ticker: string; error: string }[];
    }>("/uw-analyze/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      token,
      timeout: 120_000,
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
