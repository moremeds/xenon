import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import { readReasonCode } from "@/lib/readReasonCode";

export function isUncertainFutuManagementResponse(
  status: number,
  body: Record<string, unknown> | null,
): boolean {
  const code = readReasonCode(body);
  if (code === "FUTU_SEND_UNCERTAIN") return true;
  if (status === 0) return true;
  if (status >= 500) {
    if (code && code in ORDER_REASON_CODES) return false;
    return true;
  }
  return false;
}
