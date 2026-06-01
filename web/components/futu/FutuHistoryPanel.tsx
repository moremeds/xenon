"use client";
import { FutuEquityCurve } from "./FutuEquityCurve";
import { FutuPerformanceStats } from "./FutuPerformanceStats";
import { FutuTradeHistoryTable } from "./FutuTradeHistoryTable";
import { useFutuSync, useFutuReconciliation } from "@/lib/useFutuHistory";

const START = "2024-05-01";

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

export function FutuHistoryPanel() {
  const to = todayISO();
  const recon = useFutuReconciliation();
  const sync = useFutuSync();

  return (
    <div className="space-y-4">
      {recon.data?.current_delta != null &&
        Math.abs(recon.data.current_delta) > 25 && (
          <div
            className="rounded-sm bg-orange-950/40 border border-orange-900
                        text-orange-300 text-xs px-3 py-2"
          >
            Reconciliation mismatch: ${recon.data.current_delta.toFixed(2)}
          </div>
        )}
      <div className="flex justify-end">
        <button
          className="text-xs px-3 py-1 rounded-sm bg-violet-600/20
                     border border-violet-600/40 text-violet-300"
          disabled={sync.isPending}
          onClick={() => sync.mutate({ since: "auto" })}
        >
          {sync.isPending ? "Syncing…" : "Sync now"}
        </button>
      </div>
      <FutuPerformanceStats from={START} to={to} />
      <FutuEquityCurve from={START} to={to} />
      <FutuTradeHistoryTable from={START} to={to} />
    </div>
  );
}
