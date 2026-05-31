import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    return NextResponse.json(await xenonFetch("/position-rules"));
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
