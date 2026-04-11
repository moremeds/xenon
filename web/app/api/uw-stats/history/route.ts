import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(req: NextRequest): Promise<Response> {
  try {
    const { getToken } = await auth();
    const token = (await getToken()) ?? undefined;
    const hoursRaw = req.nextUrl.searchParams.get("hours");
    const query = hoursRaw ? `?hours=${encodeURIComponent(hoursRaw)}` : "";
    const data = await xenonFetch<Record<string, unknown>>(
      `/uw-stats/history${query}`,
      {
        method: "GET",
        token,
        timeout: 5_000,
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
