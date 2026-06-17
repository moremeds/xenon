import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(req?: Request): Promise<Response> {
  // `req` (or req.url) is absent when the route handler is unit-tested directly;
  // production Next.js always supplies a Request. Default to the IB scope.
  const broker = req?.url ? new URL(req.url).searchParams.get("broker") : null;
  const qs = broker ? `?broker=${encodeURIComponent(broker)}` : "";
  try {
    const data = await xenonFetch(`/journal${qs}`, {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to read journal";
    return NextResponse.json({ error: message, trades: [] }, { status: 500 });
  }
}
