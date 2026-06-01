import { NextRequest, NextResponse } from "next/server";
import { resolveBrowserIbRealtimeWsUrl } from "@/lib/server/ibRealtimeRuntime";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const h = request.headers;
  return NextResponse.json({
    url: resolveBrowserIbRealtimeWsUrl({
      host: h.get("host"),
      forwardedHost: h.get("x-forwarded-host"),
      forwardedProto: h.get("x-forwarded-proto"),
    }),
  });
}
