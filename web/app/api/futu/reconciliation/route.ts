import { NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET() {
  try {
    const data = await xenonFetch("/futu/reconciliation", { method: "GET" });
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
