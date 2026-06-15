import type { IbAuthVerdict, IbGatewayInfo } from "@/lib/operatorTypes";

const VERDICT_TONE: Record<IbAuthVerdict, string> = {
  authenticated: "core",
  awaiting: "warn",
  unreachable: "fault",
  unknown: "neutral",
};

export function IbGatewayCard({
  gateway,
  verdict,
  account,
  tradingMode,
  modeVerified,
}: {
  gateway: IbGatewayInfo;
  verdict: IbAuthVerdict;
  account: string;
  tradingMode: string;
  modeVerified: boolean;
}) {
  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">IB Gateway</p>
        <h3 className="panel-title">Gateway</h3>
        <span
          className={`operator-pill operator-pill--${VERDICT_TONE[verdict]}`}
        >
          {verdict}
        </span>
      </header>
      <dl className="operator-kv">
        <div>
          <dt>Host</dt>
          <dd>{`${gateway.host ?? "---"}:${gateway.port ?? "---"}`}</dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>{gateway.gateway_mode ?? "---"}</dd>
        </div>
        <div>
          <dt>Port</dt>
          <dd>{gateway.port_listening ? "listening" : "closed"}</dd>
        </div>
        <div>
          <dt>Account</dt>
          <dd>{account || "---"}</dd>
        </div>
        <div>
          <dt>Trading mode</dt>
          <dd>
            {tradingMode} {modeVerified ? "✓" : "⚠"}
          </dd>
        </div>
      </dl>
    </section>
  );
}
