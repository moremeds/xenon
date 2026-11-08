import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

/** Ack one open risk incident. Upstream status + top-level reason_code pass through. */
export async function POST(
  _request: Request,
  context: { params: Promise<{ key: string }> },
): Promise<Response> {
  const requestId = getRequestId();
  try {
    const { key } = await context.params;
    return NextResponse.json(
      await xenonFetch(`/risk/incidents/${encodeURIComponent(key)}/ack`, {
        method: "POST",
      }),
    );
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
