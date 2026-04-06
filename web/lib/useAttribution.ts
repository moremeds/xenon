"use client";

import { useCallback, useEffect, useState } from "react";
import type { AttributionData } from "./types";

export function useAttribution() {
  const [data, setData] = useState<AttributionData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAttribution = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/attribution");
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      const json = await res.json();
      setData(json);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAttribution();
  }, [fetchAttribution]);

  return { data, loading, error, refetch: fetchAttribution };
}
