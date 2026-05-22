/**
 * Stub — the regime gate was removed in the pure-portfolio pivot.
 * Backend no longer emits REGIME_BLOCK or REGIME_RESIZE_REQUIRED, so the
 * modal is unreachable in practice. Kept as a null-rendering shim so the
 * defensive call sites in OrderTab.tsx compile without surgery.
 */

import type { ReactNode } from "react";

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function RegimeBlockModal(_props: Record<string, unknown>): ReactNode {
  return null;
}
