import { NextResponse } from "next/server";

export const runtime = "nodejs";

const XENON_API = process.env.XENON_API_URL || "http://localhost:8321";

export async function GET(request: Request): Promise<Response> {
  try {
    const url = new URL(request.url);
    const sessionId = url.searchParams.get("session_id");
    const upstreamUrl = new URL(`${XENON_API}/wizard/stream`);
    if (sessionId) {
      upstreamUrl.searchParams.set("session_id", sessionId);
    }

    const upstream = await fetch(upstreamUrl.toString(), {
      headers: { Accept: "text/event-stream" },
      cache: "no-store",
      signal: request.signal,
    });

    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => `HTTP ${upstream.status}`);
      return NextResponse.json({ error: detail }, { status: upstream.status });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return new Response(null, { status: 499 });
    }
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 500 },
    );
  }
}
