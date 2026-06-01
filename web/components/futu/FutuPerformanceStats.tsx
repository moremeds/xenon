"use client";
import { useFutuPerformance } from "@/lib/useFutuHistory";

const fmtPct = (n: number) => `${(n * 100).toFixed(2)}%`;
const fmtUSD = (n: number) =>
  `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-sm border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="text-[10px] uppercase text-zinc-500">{label}</div>
      <div className="text-sm font-medium">{value}</div>
    </div>
  );
}

export function FutuPerformanceStats({
  from,
  to,
}: {
  from: string;
  to: string;
}) {
  const { data, isLoading } = useFutuPerformance({ from, to });
  if (isLoading || !data)
    return <div className="text-xs text-zinc-500">Loading…</div>;
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
      <Card label="TWR" value={fmtPct(data.total_return_twr)} />
      <Card label="CAGR" value={fmtPct(data.cagr)} />
      <Card label="Max DD" value={fmtPct(data.max_drawdown)} />
      <Card
        label="Win rate"
        value={data.win_rate != null ? fmtPct(data.win_rate) : "—"}
      />
      <Card
        label="Avg holding"
        value={
          data.avg_holding_days != null
            ? `${data.avg_holding_days.toFixed(0)}d`
            : "—"
        }
      />
      <Card
        label="Commissions"
        value={
          data.total_commissions != null ? fmtUSD(data.total_commissions) : "—"
        }
      />
      <Card
        label="Deposits"
        value={fmtUSD(data.external_cash_flows.deposits)}
      />
      <Card
        label="Withdrawals"
        value={fmtUSD(data.external_cash_flows.withdrawals)}
      />
    </div>
  );
}
