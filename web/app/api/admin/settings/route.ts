import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Runtime settings list (FastAPI GET /admin/settings, internal token via xenonFetch). */
export async function GET(): Promise<Response> {
  try {
    const data = await xenonFetch("/admin/settings", { method: "GET", timeout: 8_000 });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, getRequestId());
  }
}
