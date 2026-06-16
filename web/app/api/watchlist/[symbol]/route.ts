import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";

export const runtime = "nodejs";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ symbol: string }> },
) {
  const requestId = getRequestId();
  try {
    const { symbol } = await params;
    const data = await xenonFetch(`/watchlist/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, requestId);
  }
}
