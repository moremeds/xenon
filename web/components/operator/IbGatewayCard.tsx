import type {
  IbAuthVerdict,
  IbGatewayInfo,
  IbPoolRole,
} from "@/lib/operatorTypes";

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
  pool,
}: {
  gateway: IbGatewayInfo;
  verdict: IbAuthVerdict;
  account: string;
  tradingMode: string;
  modeVerified: boolean;
  pool: Record<string, IbPoolRole>;
}) {
  const roles = Object.entries(pool);
  return (
    <div className="operator-broker">
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
      {roles.length > 0 ? (
        <div className="operator-roles">
          {roles.map(([role, info]) => (
            <span
              key={role}
              className="operator-role"
              title={`client ${info?.client_id ?? "?"} · ${
                info?.connected ? "connected" : "disconnected"
              }`}
            >
              <span
                className={`operator-role__dot operator-role__dot--${
                  info?.connected ? "ok" : "down"
                }`}
                aria-hidden
              />
              {role}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
