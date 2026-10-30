export const IB_MFA_REQUIRED_ISSUE = "ibc_mfa_required";

const DEFAULT_MFA_APPROVAL_MESSAGE =
  "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.";

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function classifyIBConnectionError(message, options = {}) {
  const text = String(message ?? "");
  const ibHost = options.ibHost ?? "127.0.0.1";
  const ibPort = options.ibPort ?? 4001;
  const expectedTarget = `${escapeRegExp(ibHost)}:${escapeRegExp(ibPort)}`;
  const refusedPattern = new RegExp(`\\bconnect\\s+ECONNREFUSED\\s+${expectedTarget}\\b`, "i");

  if (!refusedPattern.test(text)) {
    return null;
  }

  return {
    code: IB_MFA_REQUIRED_ISSUE,
    operatorMessage: DEFAULT_MFA_APPROVAL_MESSAGE,
    technicalMessage: text,
  };
}

export function getDefaultMfaApprovalMessage() {
  return DEFAULT_MFA_APPROVAL_MESSAGE;
}
