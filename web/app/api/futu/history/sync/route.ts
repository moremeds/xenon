import { NextRequest, NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function POST(req: NextRequest) {
  const body = await req.text();
  try {
    const data = await xenonFetch("/futu/history/sync", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof XenonApiError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 },
    );
  }
}
