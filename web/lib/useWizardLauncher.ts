"use client";

import { useCallback, useState } from "react";

/**
 * useWizardLauncher — shared parent-owned wizard state for OrderBuilder and
 * ComboOrderForm.
 *
 * Both call sites used to duplicate `wizardOpen` / `wizardSessionId` useState
 * pairs and never called the setter for sessionId, meaning the modal always
 * opened with `sessionId=null`. This hook gives both parents a single,
 * stable-callback-shaped API:
 *
 *   const { sessionId, isOpen, launch, resume, close } = useWizardLauncher();
 *
 * Task 4 keeps `launch()` a pure state transition — it accepts an optional
 * sessionId so Task 5 can pass a freshly-minted id returned from
 * `/api/wizard/session` without refactoring this hook again.
 *
 * `resume()` re-opens the modal without touching sessionId (strip button use).
 */
export type UseWizardLauncherResult = {
  sessionId: string | null;
  isOpen: boolean;
  launch: (sessionId?: string | null) => void;
  resume: () => void;
  close: () => void;
};

export function useWizardLauncher(): UseWizardLauncherResult {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isOpen, setIsOpen] = useState(false);

  const launch = useCallback((nextSessionId?: string | null) => {
    if (typeof nextSessionId !== "undefined") {
      setSessionId(nextSessionId);
    }
    setIsOpen(true);
  }, []);

  const resume = useCallback(() => {
    setIsOpen(true);
  }, []);

  const close = useCallback(() => {
    setIsOpen(false);
  }, []);

  return { sessionId, isOpen, launch, resume, close };
}
