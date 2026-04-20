import { NextRequest, NextResponse } from "next/server";
import { resolveBrowserIbRealtimeWsUrl } from "@/lib/server/ibRealtimeRuntime";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  return NextResponse.json({
    url: resolveBrowserIbRealtimeWsUrl({
      requestUrl: request.nextUrl.toString(),
    }),
  });
}
