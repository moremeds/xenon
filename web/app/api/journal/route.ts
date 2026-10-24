import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

export async function GET(req?: Request): Promise<Response> {
  const qs = forwardRequestQuery(req);
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
