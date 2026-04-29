import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  try {
    const data = await xenonFetch("/blotter", { method: "GET", timeout: 10_000 });
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to read blotter";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export async function POST(): Promise<Response> {
  try {
    const data = await xenonFetch("/blotter", { method: "POST", timeout: 130_000 });
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Blotter sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
