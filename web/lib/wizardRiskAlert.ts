export type WizardRiskAlertState = "CROSSED" | "NO_QUOTE";

export type WizardRiskAlert = {
  session_id: string;
  ticker: string;
  state: WizardRiskAlertState;
  threshold: string;
  quote: string | null;
  quote_as_of: string | null;
  evaluated_at: string;
};

export type WizardAlertsResponse = {
  alerts: WizardRiskAlert[];
  events: Array<{
    session_id: string;
    kind: string;
    ticker: string;
    at: string | null;
    detail: Record<string, unknown> | null;
  }>;
  monitor: { running: boolean; last_run_at: string | null };
};
