import { NextResponse } from "next/server";

import { getRequestId } from "@/lib/apiContracts";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ key: string }> };

// Registry keys are env-style names or snake_case; anything else never
// reaches the API (no path tricks through the proxy).
const KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

function refuse(status: number, reason_code: string, detail: string): Response {
  return NextResponse.json({ reason_code, detail }, { status });
}

/** PUT /admin/settings/{key} — forwards only {value, note?, confirm?}. */
export async function PUT(request: Request, { params }: Ctx): Promise<Response> {
  const { key } = await params;
  if (!KEY_RE.test(key)) return refuse(400, "SETTING_UNKNOWN", "malformed setting key");
  let body: Record<string, unknown> | null = null;
  try {
    const parsed = await request.json();
    body = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    body = null;
  }
  if (!body || !("value" in body)) {
    return refuse(400, "SETTING_INVALID_VALUE", "body must be a JSON object with `value`");
  }
  const out: Record<string, unknown> = { value: body.value };
  if (typeof body.note === "string") out.note = body.note;
  if (typeof body.confirm === "string") out.confirm = body.confirm;
  try {
    const data = await xenonFetch(`/admin/settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(out),
      timeout: 10_000,
    });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, getRequestId());
  }
}

/** DELETE /admin/settings/{key} — revert to the env value; forwards note / confirm. */
export async function DELETE(request: Request, { params }: Ctx): Promise<Response> {
  const { key } = await params;
  if (!KEY_RE.test(key)) return refuse(400, "SETTING_UNKNOWN", "malformed setting key");
  const incoming = new URL(request.url).searchParams;
  const qs = new URLSearchParams();
  for (const name of ["note", "confirm"]) {
    const v = incoming.get(name);
    if (v != null) qs.set(name, v);
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  try {
    const data = await xenonFetch(`/admin/settings/${key}${suffix}`, { method: "DELETE", timeout: 10_000 });
    return NextResponse.json(data);
  } catch (e) {
    return passThroughXenonError(e, getRequestId());
  }
}
