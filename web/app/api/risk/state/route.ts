import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Tail-risk guard state (FastAPI lifespan task) + recent risk_events. */
export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    return NextResponse.json(
      await xenonFetch("/risk/state", { timeout: 8_000 }),
    );
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
