import type { FutuInfo } from "@/lib/operatorTypes";

function fmtAgeShort(secs: number | null | undefined): string {
  if (secs == null) return "—";
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

/**
 * Futu OpenD broker section — mirrors IbGatewayCard so the two brokers read the
 * same way inside the shared BrokersCard. Futu is read-only: it does a positions
 * `sync` but cannot place `orders` (no order/quote support via the local OpenD).
 * The orders pill is rendered as a dashed, muted "not supported" pill rather
 * than a live/down dot.
 */
export function FutuGatewayCard({ futu }: { futu: FutuInfo }) {
  const verdict = !futu.configured
    ? { label: "off", tone: "neutral" }
    : futu.connected
      ? { label: "connected", tone: "core" }
      : { label: "idle", tone: "neutral" };

  return (
    <div className="operator-broker">
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Futu</p>
        <h3 className="panel-title">OpenD</h3>
        <span className={`operator-pill operator-pill--${verdict.tone}`}>
          {verdict.label}
        </span>
      </header>
      <dl className="operator-kv">
        <div>
          <dt>Access</dt>
          <dd>read-only</dd>
        </div>
        <div>
          <dt>Last sync</dt>
          <dd>{fmtAgeShort(futu.last_sync_age_s)}</dd>
        </div>
      </dl>
      <div className="operator-roles">
        <span
          className="operator-role"
          title={futu.connected ? "connected" : "disconnected"}
        >
          <span
            className={`operator-role__dot operator-role__dot--${
              futu.connected ? "ok" : "down"
            }`}
            aria-hidden
          />
          sync
        </span>
        <span
          className="operator-role operator-role--off"
          title="not supported — Futu is read-only"
        >
          <span
            className="operator-role__dot operator-role__dot--off"
            aria-hidden
          />
          orders
        </span>
      </div>
    </div>
  );
}
