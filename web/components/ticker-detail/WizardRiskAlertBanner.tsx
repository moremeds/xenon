"use client";

import { useEffect, useState } from "react";

import type {
  WizardAlertsResponse,
  WizardRiskAlert,
} from "@/lib/wizardRiskAlert";

const POLL_MS = 15_000;

function alertText(a: WizardRiskAlert): string {
  if (a.state === "CROSSED") {
    return `${a.ticker} net mid ${a.quote ?? "---"} crossed alert line ${a.threshold}. Assisted Exit: confirm the close manually.`;
  }
  return `${a.ticker} no fresh quote (older than 2 min). Alert line ${a.threshold} not evaluated.`;
}

/**
 * WizardRiskAlertBanner — renders the Wizard Risk Alert states from the
 * FastAPI lifespan task (`GET /wizard/alerts`). Read-only: it never places,
 * cancels or modifies an order. Renders nothing when no alert is active.
 */
export default function WizardRiskAlertBanner() {
  const [alerts, setAlerts] = useState<WizardRiskAlert[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/api/wizard/alerts", { cache: "no-store" });
        if (!res.ok) return;
        const body = (await res.json()) as WizardAlertsResponse;
        if (alive) setAlerts(Array.isArray(body.alerts) ? body.alerts : []);
      } catch {
        // Keep the last known state; the next poll retries.
      }
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (alerts.length === 0) return null;

  return (
    <div className="wizard-risk-alert-banner" role="alert">
      {alerts.map((a) => (
        <div
          key={a.session_id}
          className={`wizard-risk-alert wizard-risk-alert--${a.state === "CROSSED" ? "crossed" : "no-quote"}`}
          data-testid={`wizard-risk-alert-${a.session_id}`}
        >
          <span className="wizard-risk-alert__label">
            {a.state === "CROSSED" ? "RISK ALERT" : "RISK ALERT · NO QUOTE"}
          </span>
          <span className="wizard-risk-alert__text">{alertText(a)}</span>
        </div>
      ))}
    </div>
  );
}
