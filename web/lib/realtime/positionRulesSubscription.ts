"use client";

import { useEffect } from "react";

const POSITION_RULES_CHANNEL = "position_rule.transition";
const POSITION_RULES_REALTIME_URL = `/api/realtime?channel=${POSITION_RULES_CHANNEL}`;

export function subscribePositionRulesRealtime(onEvent: () => void): () => void {
  if (typeof EventSource === "undefined") return () => {};

  const source = new EventSource(POSITION_RULES_REALTIME_URL);
  source.onmessage = onEvent;

  return () => source.close();
}

export function usePositionRulesRealtime(onEvent: (() => void) | null) {
  useEffect(() => {
    if (!onEvent) return undefined;
    return subscribePositionRulesRealtime(onEvent);
  }, [onEvent]);
}
