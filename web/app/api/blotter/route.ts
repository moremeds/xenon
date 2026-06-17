import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

function brokerQuery(req?: Request): string {
  // `req` (or req.url) is absent when the route handler is unit-tested directly;
  // production Next.js always supplies a Request. Default to the IB scope.
  if (!req?.url) return "";
  const broker = new URL(req.url).searchParams.get("broker");
  return broker ? `?broker=${encodeURIComponent(broker)}` : "";
}

export async function GET(req?: Request): Promise<Response> {
  try {
    const data = await xenonFetch(`/blotter${brokerQuery(req)}`, {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to read blotter";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export async function POST(req?: Request): Promise<Response> {
  try {
    const data = await xenonFetch(`/blotter${brokerQuery(req)}`, {
      method: "POST",
      timeout: 130_000,
    });
    return NextResponse.json(data);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Blotter sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
