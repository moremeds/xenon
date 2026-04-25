import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const requestId = getRequestId();
  try {
    const { id } = await context.params;
    return NextResponse.json(await xenonFetch(`/wizard/sessions/${id}`));
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
