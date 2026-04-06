"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";

export function useTickerNav() {
  const router = useRouter();

  const navigateToTicker = useCallback(
    (ticker: string, positionId?: number) => {
      const path = `/${ticker.toUpperCase()}`;
      const qs = positionId != null ? `?posId=${positionId}` : "";
      router.push(`${path}${qs}`);
    },
    [router],
  );

  return { navigateToTicker };
}
