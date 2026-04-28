import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

/**
 * POST /api/journal/sync
 *
 * Periodic file sync is retired. FastAPI reports pending PG outbox work.
 */
export async function POST(): Promise<Response> {
  try {
    const result = await xenonFetch("/journal/sync", {
      method: "POST",
      timeout: 10_000,
    });
    return NextResponse.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Sync failed";
    return NextResponse.json({ error: message, imported: 0, skipped: 0 }, { status: 500 });
  }
}
