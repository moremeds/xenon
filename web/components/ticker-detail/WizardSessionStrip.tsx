"use client";

import type { UseWizardSessionResult } from "@/lib/useWizardSession";

type Props = {
  sessionId: string | null;
  /**
   * Session state pulled from `useWizardSession()` in the parent. Shared with
   * WizardModal so a single SSE stream powers both surfaces.
   */
  session: UseWizardSessionResult;
  onResume: () => void;
};

/**
 * WizardSessionStrip — monitor/resume-only bar shown in the parent surface
 * (OrderBuilder, ComboOrderForm) whenever a wizard session is active. It does
 * NOT perform the workflow itself; the "Resume Wizard" button re-opens the
 * popup WizardModal where submit/reprice/protect actions live.
 *
 * Renders nothing when no session is active to avoid visual noise.
 */
export default function WizardSessionStrip({
  sessionId,
  session: sessionResult,
  onResume,
}: Props) {
  const { session } = sessionResult;

  if (!sessionId || !session) {
    return null;
  }

  const label = session.structure_name
    ? `Wizard Session · ${session.structure_name} · ${session.state}`
    : `Wizard Session · ${session.state}`;

  return (
    <div className="wizard-session-strip" aria-live="polite">
      <span className="wizard-session-strip-label">{label}</span>
      <button
        type="button"
        className="btn-secondary wizard-session-strip-resume"
        onClick={onResume}
      >
        Resume Wizard
      </button>
    </div>
  );
}
