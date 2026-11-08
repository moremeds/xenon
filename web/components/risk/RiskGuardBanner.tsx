"use client";

import { useCallback, useEffect, useState } from "react";

const POLL_MS = 5_000;

type Incident = {
  key: string;
  incident_id: string;
  severity: string;
  reasons: string[];
  acked: boolean;
  clearing: boolean;
};

type Source = { last: number | null; age_s: number | null; fresh: boolean };

type Account = {
  nlv: number | null;
  cushion: number | null;
  leverage: number | null;
  width_ratio: number | null;
  drawdown: number | null;
  drawdown_adjusted: boolean | null;
  snapshot_age_s: number;
};

export type RiskState = {
  running: boolean;
  enabled?: boolean;
  tier?: string;
  incidents?: Incident[];
  sources?: Record<string, Source>;
  account?: Account | null;
};

const pct = (v: number | null | undefined) =>
  v == null ? "---" : `${(v * 100).toFixed(2)}%`;
const times = (v: number | null | undefined) =>
  v == null ? "---" : `${v.toFixed(2)}x`;
const usd = (v: number | null | undefined) =>
  v == null ? "---" : `$${Math.round(v).toLocaleString("en-US")}`;
const age = (v: number | null | undefined) =>
  v == null ? "never" : `${Math.round(v)} s`;

const isBlind = (i: Incident) => i.key.startsWith("health:");

/**
 * RiskGuardBanner — tail-risk guard incidents (GET /risk/state) with ack, plus
 * a details panel (account ratios, source ages). Alerts only: it never places,
 * cancels or modifies an order. Renders nothing while no incident is open.
 */
export default function RiskGuardBanner() {
  const [state, setState] = useState<RiskState | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/risk/state", { cache: "no-store" });
      if (res.ok) setState((await res.json()) as RiskState);
    } catch {
      // Keep the last known state; the next poll retries.
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const ack = async (key: string) => {
    setError(null);
    const res = await fetch(
      `/api/risk/incidents/${encodeURIComponent(key)}/ack`,
      // middleware.ts refuses a non-GET /api request without a JSON content type
      { method: "POST", headers: { "Content-Type": "application/json" } },
    );
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as {
        reason_code?: string;
        detail?: string;
      };
      setError(
        `Ack failed: ${body.reason_code ?? res.status}${body.detail ? ` (${body.detail})` : ""}`,
      );
    }
    await load();
  };

  const incidents = state?.running ? (state.incidents ?? []) : [];
  if (incidents.length === 0) return null;
  const a = state?.account;

  return (
    <div
      className="risk-guard-banner"
      role="alert"
      data-testid="risk-guard-banner"
    >
      {incidents.map((i) => (
        <div
          key={i.key}
          className={`risk-guard-incident risk-guard-incident--${isBlind(i) ? "blind" : i.severity.toLowerCase()}`}
          data-testid={`risk-incident-${i.key}`}
        >
          <span className="risk-guard-incident__label">
            {isBlind(i) ? `BLIND · ${i.key.slice(7)}` : i.severity}
          </span>
          <span className="risk-guard-incident__text">
            {i.reasons.join("; ") || "no detail"}
            {i.clearing ? " · clearing" : ""}
          </span>
          {i.acked ? (
            <span className="risk-guard-incident__acked">ACKED</span>
          ) : (
            <button
              type="button"
              className="risk-guard-incident__ack"
              onClick={() => ack(i.key)}
            >
              ACK
            </button>
          )}
        </div>
      ))}
      <div className="risk-guard-meta">
        <span>
          No automatic protection (P1 alerts only). Close in Xenon or TWS.
        </span>
        <button
          type="button"
          className="risk-guard-meta__toggle"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "HIDE DETAILS" : "DETAILS"}
        </button>
        {error ? <span className="risk-guard-meta__error">{error}</span> : null}
      </div>
      {open ? (
        <div className="risk-guard-panel" data-testid="risk-guard-panel">
          {Object.entries(state?.sources ?? {}).map(([name, s]) => (
            <span key={name}>
              {name} {s.last ?? "---"} · tick {age(s.age_s)}
              {s.fresh ? "" : " · STALE"}
            </span>
          ))}
          {a ? (
            <>
              <span>NLV {usd(a.nlv)}</span>
              <span>Cushion {pct(a.cushion)}</span>
              <span>Leverage {times(a.leverage)}</span>
              <span>Width/NLV {times(a.width_ratio)}</span>
              <span>
                Drawdown {pct(a.drawdown)}
                {a.drawdown_adjusted === false ? " (unadjusted)" : ""}
              </span>
              <span>Snapshot {age(a.snapshot_age_s)} old</span>
            </>
          ) : (
            <span>No account snapshot read</span>
          )}
        </div>
      ) : null}
    </div>
  );
}
