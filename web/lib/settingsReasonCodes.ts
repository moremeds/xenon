// Toast copy for /api/admin/settings refusals. Reads the top-level
// reason_code (readReasonCode also accepts the {detail:{reason_code}} shape).
// Validation codes keep the API detail: it names the key and the allowed range.
import { readReasonCode } from "@/lib/readReasonCode";

const COPY: Record<string, string> = {
  SETTING_UNKNOWN: "Unknown setting.",
  SETTING_INVALID_VALUE: "Invalid value",
  SETTING_WIDENS_ENV: "Narrow-only: the value would widen what the env allows",
  SETTING_CONFIRM_REQUIRED: "This change needs a typed confirm.",
  READ_ONLY_MODE: "The API is read-only (XENON_READ_ONLY=1): settings cannot change.",
  API_KEY_FORBIDDEN: "Settings need the internal token; an API key is not accepted.",
  INTERNAL_TOKEN_REQUIRED: "Settings need the internal token (check XENON_INTERNAL_API_TOKEN on the web server).",
};
const WITH_DETAIL = new Set(["SETTING_INVALID_VALUE", "SETTING_WIDENS_ENV"]);

export function settingsErrorMessage(body: unknown, fallback: string): string {
  const code = readReasonCode(body);
  const detail =
    body && typeof body === "object" && typeof (body as Record<string, unknown>).detail === "string"
      ? ((body as Record<string, unknown>).detail as string)
      : "";
  if (code && COPY[code]) {
    return WITH_DETAIL.has(code) && detail ? `${COPY[code]}: ${detail}` : COPY[code];
  }
  return detail || fallback;
}
