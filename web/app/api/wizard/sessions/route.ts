import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    return NextResponse.json(await xenonFetch("/wizard/sessions"));
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}

export async function POST(request: Request): Promise<Response> {
  const requestId = getRequestId();
  try {
    const body = await request.json();
    return NextResponse.json(
      await xenonFetch("/wizard/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
