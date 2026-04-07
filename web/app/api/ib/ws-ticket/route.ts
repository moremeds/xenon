import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export async function POST() {
  try {
    const data = await xenonFetch<{ ticket: string }>("/ws-ticket", {
      method: "POST",
    });
    return NextResponse.json(data);
  } catch (err) {
    const status = (err as { status?: number }).status ?? 500;
    const detail = err instanceof Error ? err.message : "WS ticket failed";
    return NextResponse.json({ detail }, { status });
  }
}
