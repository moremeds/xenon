import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";

export const runtime = "nodejs";

export async function GET() {
  const requestId = getRequestId();
  try {
    const data = await xenonFetch("/watchlist", {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}

export async function POST(request: Request) {
  const requestId = getRequestId();
  try {
    const body = await request.json();
    const data = await xenonFetch("/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}
