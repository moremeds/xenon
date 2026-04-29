import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  try {
    const data = await xenonFetch("/journal", { method: "GET", timeout: 10_000 });
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to read journal";
    return NextResponse.json({ error: message, trades: [] }, { status: 500 });
  }
}
