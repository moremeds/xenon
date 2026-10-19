import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Wizard Risk Alert states (FastAPI lifespan task) + persisted crossings. */
export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    return NextResponse.json(await xenonFetch("/wizard/alerts"));
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
