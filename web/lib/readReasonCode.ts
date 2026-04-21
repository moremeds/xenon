// Reads the FastAPI reason_code out of an error body. FastAPI returns
// HTTPException(detail={reason_code: ...}), which Next.js preserves as
// {detail: {reason_code: ...}}. Some legacy routes flatten it to the root,
// so we check both shapes.

export function readReasonCode(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  if (typeof b.reason_code === "string") return b.reason_code;
  const detail = b.detail;
  if (
    detail &&
    typeof detail === "object" &&
    typeof (detail as Record<string, unknown>).reason_code === "string"
  ) {
    return (detail as Record<string, unknown>).reason_code as string;
  }
  return null;
}
