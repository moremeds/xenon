"use client";

import { useCallback, useEffect, useState } from "react";

export type BrokerAccount = "ib" | "futu";

const STORAGE_KEY = "xenon.activeAccount";

/**
 * Hook: manages the IB ↔ Futu tab state with localStorage persistence.
 *
 * State lives at the `WorkspaceShell` level; no React context needed because
 * the only consumers are the shell itself (for branching the data source)
 * and the `AccountTabBar` child (which receives value + setter as props).
 *
 * Cold load default: `"ib"` — preserves the current behavior for anyone
 * who has never interacted with the switcher.
 */
export function useActiveAccount() {
  const [activeAccount, setActiveAccountState] = useState<BrokerAccount>("ib");
  const [hydrated, setHydrated] = useState(false);

  // Hydrate from localStorage on mount — can't read during SSR.
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "ib" || stored === "futu") {
        setActiveAccountState(stored);
      }
    } catch {
      // localStorage may be unavailable (Safari private mode, etc.)
    }
    setHydrated(true);
  }, []);

  const setActiveAccount = useCallback((next: BrokerAccount) => {
    setActiveAccountState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Tab choice is lost on reload if storage is unavailable — not fatal.
    }
  }, []);

  return { activeAccount, setActiveAccount, hydrated };
}
