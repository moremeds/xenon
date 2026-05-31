"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchPositionRules, type PositionRule } from "@/lib/api/positionRules";
import { usePositionRulesRealtime } from "@/lib/realtime/positionRulesSubscription";

const REFRESH_INTERVAL_MS = 30_000;

export function usePositionRules(active = true): {
  rules: PositionRule[];
  error: string | null;
  refresh: () => void;
} {
  const [rules, setRules] = useState<PositionRule[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!active) return;
    void fetchPositionRules()
      .then((next) => {
        setRules(next);
        setError(null);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [active]);

  useEffect(() => {
    if (!active) {
      setRules([]);
      setError(null);
      return undefined;
    }

    refresh();
    const timer = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  usePositionRulesRealtime(active ? refresh : null);

  return { rules, error, refresh };
}
