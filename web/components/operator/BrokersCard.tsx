import type { OperatorData } from "@/lib/operatorTypes";

import { FutuGatewayCard } from "./FutuGatewayCard";
import { IbGatewayCard } from "./IbGatewayCard";

/**
 * Unified Brokers card — IB Gateway and Futu OpenD share one panel, split by a
 * vertical divider. Each broker keeps its own eyebrow/title/verdict + role
 * pills; merging them removes the second panel chrome so the two read as one
 * connectivity surface.
 */
export function BrokersCard({ data }: { data: OperatorData }) {
  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <div className="operator-brokers">
        <IbGatewayCard
          gateway={data.ib_gateway}
          verdict={data.ib_auth}
          account={data.account}
          tradingMode={data.trading_mode}
          modeVerified={data.mode_verified}
          pool={data.ib_pool}
        />
        <FutuGatewayCard futu={data.futu} />
      </div>
    </section>
  );
}
