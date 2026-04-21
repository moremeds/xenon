import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const ticker = url.searchParams.get("ticker");
  const conId = url.searchParams.get("con_id");
  if (!ticker || !conId) {
    return NextResponse.json(
      { error: "ticker and con_id required" },
      { status: 400 },
    );
  }
  const upstream = await xenonFetch(
    `/orders/quote?ticker=${encodeURIComponent(ticker)}&con_id=${encodeURIComponent(conId)}`,
  );
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
