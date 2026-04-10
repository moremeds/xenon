import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

export const runtime = "nodejs";

const XENON_API = process.env.XENON_API_URL || "http://localhost:8321";

export async function GET(request: Request): Promise<Response> {
  try {
    const { getToken } = await auth();
    const token = (await getToken()) ?? undefined;
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
    };
    if (token) headers.Authorization = `Bearer ${token}`;

    const upstream = await fetch(`${XENON_API}/uw-analyze/portfolio`, {
      headers,
      cache: "no-store",
      signal: request.signal,
    });

    if (!upstream.ok) {
      const detail = await upstream
        .text()
        .catch(() => `HTTP ${upstream.status}`);
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
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return new Response(null, { status: 499 });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Unknown error" },
      { status: 500 },
    );
  }
}
