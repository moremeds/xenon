export type ConnectionBannerTone = "error" | "warning" | "success";

export type ConnectionBannerState = {
  tone: ConnectionBannerTone;
  message: string;
};

export type ConnectionBannerInput = {
  reconnected: boolean;
  wsConnected: boolean;
  ibConnected: boolean;
  ibIssue: string | null;
  ibStatusMessage: string | null;
};

const DEFAULT_MFA_APPROVAL_MESSAGE =
  "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.";

export function getConnectionBannerState(
  input: ConnectionBannerInput,
): ConnectionBannerState | null {
  if (input.ibIssue === "ibc_mfa_required") {
    return {
      tone: "warning",
      message: input.ibStatusMessage ?? DEFAULT_MFA_APPROVAL_MESSAGE,
    };
  }

  if (input.ibConnected && !input.wsConnected) {
    return {
      tone: "warning",
      message:
        "Live data stream offline — IB Gateway is still connected; prices may be delayed.",
    };
  }

  return null;
}
