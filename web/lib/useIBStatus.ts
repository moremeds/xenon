"use client";

/**
 * Re-export IBStatus from the shared context.
 * Previously, each useIBStatus() call created its own WebSocket.
 * Now all consumers share a single connection via IBStatusProvider.
 */
export { useIBStatusContext as useIBStatus } from "./IBStatusContext";
export type { IBStatusState as IBStatus } from "./IBStatusContext";
