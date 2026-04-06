import { NextResponse } from "next/server";
import { runScript } from "@tools/runner";

export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  try {
    const { searchParams } = new URL(request.url);
    const ticker = searchParams.get("ticker");

    if (!ticker) {
      return NextResponse.json({ error: "ticker parameter required" }, { status: 400 });
    }

    const result = await runScript("scripts/fetch_analyst_ratings.py", {
      args: [ticker.toUpperCase(), "--json"],
      timeout: 30_000,
    });

    if (!result.ok) {
      return NextResponse.json(
        { error: "Failed to fetch ratings", stderr: result.stderr },
        { status: 502 },
      );
    }

    return NextResponse.json(result.data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to fetch ratings";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
