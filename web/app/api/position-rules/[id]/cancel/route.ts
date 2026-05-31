import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const requestId = getRequestId();
  const { id } = await context.params;
  try {
    return NextResponse.json(
      await xenonFetch(`/position-rules/${id}/cancel`, { method: "POST" }),
    );
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
