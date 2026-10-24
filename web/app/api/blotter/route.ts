import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

export async function GET(req?: Request): Promise<Response> {
  const qs = forwardRequestQuery(req);
  try {
    const data = await xenonFetch(`/blotter${qs}`, {
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
  const qs = forwardRequestQuery(req);
  try {
    const data = await xenonFetch(`/blotter${qs}`, {
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
