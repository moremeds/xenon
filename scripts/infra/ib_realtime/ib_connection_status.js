export const IB_MFA_REQUIRED_ISSUE = "ibc_mfa_required";

const DEFAULT_MFA_APPROVAL_MESSAGE =
  "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.";

// Node's `net` module emits these errors with the resolved IP in the text,
// not the hostname we pass to ib.connect(). When the relay runs in Docker
// with IB_GATEWAY_HOST=host.docker.internal, the message reads
// "connect ECONNREFUSED 192.168.5.2:4001" — interpolating the configured
// host into the pattern silently misses, killing the reconnect loop.
// Match the error code family instead; only the IB socket emits these here.
const CONNECT_ERROR_PATTERN =
  /\bconnect\s+(ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|EADDRNOTAVAIL|ENOTFOUND|EAI_AGAIN)\b/i;

export function classifyIBConnectionError(message) {
  const text = String(message ?? "");
  if (!CONNECT_ERROR_PATTERN.test(text)) return null;
  return {
    code: IB_MFA_REQUIRED_ISSUE,
    operatorMessage: DEFAULT_MFA_APPROVAL_MESSAGE,
    technicalMessage: text,
  };
}

export function getDefaultMfaApprovalMessage() {
  return DEFAULT_MFA_APPROVAL_MESSAGE;
}

// IB "informational" notification codes. Under @stoqey/ib these arrive on
// EventName.info (and some still on EventName.error); they are data-farm
// connect/OK chatter and must NEVER flip ib_connected. The `/farm connection is
// OK/i` regex in the relay error handler is kept as a belt-and-suspenders
// fallback for any informational text not covered by this list.
const INFO_CODES = new Set([
  1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2157, 2158,
]);

export function isInfoCode(code) {
  return INFO_CODES.has(Number(code));
}
