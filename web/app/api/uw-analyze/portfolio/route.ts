import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import type { UwPortfolioResponse } from "@/lib/uwAnalyzeTypes";

export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  try {
    const { getToken } = await auth();
    const token = (await getToken()) ?? undefined;
    const data = await xenonFetch<UwPortfolioResponse>(
      "/uw-analyze/portfolio",
      {
        method: "GET",
        token,
        timeout: 120_000,
      },
    );
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
