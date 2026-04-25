"use client";

import Modal from "@/components/Modal";
import type { UseWizardSessionResult } from "@/lib/useWizardSession";

type Props = {
  open: boolean;
  sessionId: string | null;
  ticker: string;
  /**
   * Session state pulled from `useWizardSession()` in the parent. Passing it
   * down (instead of subscribing again inside the modal) keeps the SSE stream
   * count at one per session — matching the WizardSessionStrip contract.
   */
  session: UseWizardSessionResult;
  onClose: () => void;
  onSubmit?: () => void;
  onRepriceNatural?: () => void;
  onAbort?: () => void;
};

type StepId = "plan" | "submit" | "reprice" | "protect" | "fill";

const STEPS: ReadonlyArray<{ id: StepId; label: string }> = [
  { id: "plan", label: "Plan" },
  { id: "submit", label: "Submit" },
  { id: "reprice", label: "Reprice" },
  { id: "protect", label: "Protect" },
  { id: "fill", label: "Fill" },
];

/**
 * Unambiguous mapping from session state → active step id. Replaces the old
 * `state.toLowerCase().includes(step.id)` heuristic, which lit up multiple
 * steps for compound states like `REPRICE_PROTECT_PENDING` (matches both
 * `reprice` AND `protect`).
 *
 * Any unmapped state falls back to the first step (Plan) and logs a dev-only
 * warning — see `resolveActiveStep()`.
 */
const STATE_TO_STEP_ID: Record<string, StepId> = {
  IDLE: "plan",
  LOADING: "plan",
  DRAFT: "plan",
  PLAN: "plan",
  PLANNING: "plan",
  PLANNED: "plan",
  READY: "plan",
  SUBMIT: "submit",
  SUBMITTING: "submit",
  SUBMITTED: "submit",
  WORKING: "submit",
  PENDING_SUBMIT: "submit",
  REPRICE: "reprice",
  REPRICING: "reprice",
  REPRICE_PENDING: "reprice",
  REPRICE_PROTECT_PENDING: "reprice",
  PROTECT: "protect",
  PROTECTING: "protect",
  PROTECT_PENDING: "protect",
  PROTECTION_PENDING: "protect",
  FILL: "fill",
  FILLED: "fill",
  FILLING: "fill",
  PARTIALLY_FILLED: "fill",
  DONE: "fill",
  COMPLETE: "fill",
  COMPLETED: "fill",
};

const warnedStates = new Set<string>();

function resolveActiveStep(state: string): StepId {
  const normalizedState = state.toUpperCase();
  const mapped = STATE_TO_STEP_ID[normalizedState];
  if (mapped) return mapped;
  if (process.env.NODE_ENV !== "production" && !warnedStates.has(state)) {
    warnedStates.add(state);
    console.warn(
      `[WizardModal] Unmapped session state "${state}" — falling back to "plan". Add it to STATE_TO_STEP_ID.`,
    );
  }
  return "plan";
}

/**
 * WizardModal — popup modal dialog hosting the combo wizard workflow.
 *
 * Rendered via shared `Modal` primitive (role=dialog, aria-modal, portal,
 * semi-transparent scrim so the underlying ticker page stays visible). Layout
 * follows the spec: header telemetry rail, step strip, main pane + right
 * telemetry rail, sticky footer action rail. Collapses to single column on
 * narrow viewports via `.wizard-panel` media query.
 *
 * Signed combo prices render as-is (credits negative, debits positive). This
 * component never applies `Math.abs()` — display values come straight from
 * the `session` prop.
 */
export default function WizardModal({
  open,
  sessionId,
  ticker,
  session: sessionResult,
  onClose,
  onSubmit,
  onRepriceNatural,
  onAbort,
}: Props) {
  if (!open) return null;

  const { session, loading, error } = sessionResult;
  const state = session?.state ?? (loading ? "LOADING" : "IDLE");
  const activeStepId = resolveActiveStep(state);
  const structureName = session?.structure_name ?? "—";
  const netLabel =
    typeof session?.net_price === "number"
      ? (session.net_price as number).toFixed(2)
      : "—";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Combo Wizard"
      className="wizard-modal"
    >
      <div className="wizard-panel">
        <div className="wizard-meta-rail">
          <span>MODE COMBO</span>
          <span>NATURAL/MID LADDER</span>
          <span>SERVER AUTHORITY</span>
          <span>TICKER {ticker}</span>
          <span>SESSION {sessionId ?? "—"}</span>
        </div>

        <ol className="wizard-step-strip" role="list" aria-label="Wizard steps">
          {STEPS.map((step) => {
            const isActive = step.id === activeStepId;
            return (
              <li
                key={step.id}
                aria-current={isActive ? "step" : undefined}
                className={`wizard-step${isActive ? " wizard-step-active" : ""}`}
              >
                {step.label}
              </li>
            );
          })}
        </ol>

        <div className="wizard-body">
          <section className="wizard-main-pane" aria-label="Wizard workflow">
            <div className="wizard-main-row">
              <span className="wizard-main-key">STRUCTURE</span>
              <span className="wizard-main-value">{structureName}</span>
            </div>
            <div className="wizard-main-row">
              <span className="wizard-main-key">STATE</span>
              <span className="wizard-main-value">{state}</span>
            </div>
            <div className="wizard-main-row">
              <span className="wizard-main-key">NET</span>
              <span className="wizard-main-value">{netLabel}</span>
            </div>
            {error && (
              <div className="wizard-main-row" role="alert">
                <span className="wizard-main-key">ERROR</span>
                <span className="wizard-main-value">{error}</span>
              </div>
            )}
          </section>

          <aside
            className="wizard-telemetry-rail"
            aria-label="Wizard telemetry"
          >
            <div className="wizard-telemetry-row">
              <span className="wizard-telemetry-key">TIF</span>
              <span className="wizard-telemetry-value">
                {typeof session?.tif === "string"
                  ? (session.tif as string)
                  : "DAY"}
              </span>
            </div>
            <div className="wizard-telemetry-row">
              <span className="wizard-telemetry-key">QTY</span>
              <span className="wizard-telemetry-value">
                {typeof session?.quantity === "number"
                  ? (session.quantity as number)
                  : "—"}
              </span>
            </div>
            <div className="wizard-telemetry-row">
              <span className="wizard-telemetry-key">ATTEMPT</span>
              <span className="wizard-telemetry-value">
                {typeof session?.attempt === "number"
                  ? (session.attempt as number)
                  : "—"}
              </span>
            </div>
          </aside>
        </div>

        <div className="wizard-footer-rail">
          <button
            type="button"
            className="btn-secondary"
            onClick={onAbort}
            disabled={!onAbort}
          >
            Abort
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={onRepriceNatural}
            disabled={!onRepriceNatural}
          >
            Reprice Natural
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onSubmit}
            disabled={!onSubmit}
          >
            Submit
          </button>
        </div>
      </div>
    </Modal>
  );
}
