import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const requestId = getRequestId();
  try {
    const { id } = await context.params;
    const body = await request.json();
    return NextResponse.json(
      await xenonFetch(`/wizard/sessions/${id}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
