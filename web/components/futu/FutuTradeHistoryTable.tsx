"use client";
import { useState } from "react";
import { useFutuTrades } from "@/lib/useFutuHistory";

interface Props {
  from: string;
  to: string;
}

export function FutuTradeHistoryTable({ from, to }: Props) {
  const [ticker, setTicker] = useState("");
  const [action, setAction] = useState<"" | "BUY" | "SELL">("");
  const [cursor, setCursor] = useState<string | undefined>();
  const { data, isLoading } = useFutuTrades({
    from,
    to,
    ticker: ticker || undefined,
    action: action || undefined,
    sort: "filled_at",
    direction: "desc",
    limit: 100,
    cursor,
  });

  return (
    <div className="rounded-sm border border-zinc-800 bg-zinc-950">
      <div className="flex gap-2 p-3 border-b border-zinc-800">
        <input
          className="bg-zinc-900 text-sm px-2 py-1 rounded-sm"
          placeholder="Ticker"
          value={ticker}
          onChange={(e) => {
            setTicker(e.target.value.toUpperCase());
            setCursor(undefined);
          }}
        />
        <select
          className="bg-zinc-900 text-sm px-2 py-1 rounded-sm"
          value={action}
          onChange={(e) => {
            setAction(e.target.value as "" | "BUY" | "SELL");
            setCursor(undefined);
          }}
        >
          <option value="">All actions</option>
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
      </div>
      <table className="w-full text-xs">
        <thead className="text-zinc-500">
          <tr>
            <th className="text-left p-2">Filled</th>
            <th className="text-left p-2">Ticker</th>
            <th className="text-left p-2">Action</th>
            <th className="text-right p-2">Qty</th>
            <th className="text-right p-2">Price</th>
            <th className="text-right p-2">Fees</th>
            <th className="text-right p-2">Realized P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {isLoading && (
            <tr>
              <td className="p-2" colSpan={7}>
                Loading…
              </td>
            </tr>
          )}
          {data?.rows.map((r) => (
            <tr key={r.futu_deal_id} className="border-t border-zinc-900">
              <td className="p-2">{r.filled_at.slice(0, 19)}</td>
              <td className="p-2">{r.ticker}</td>
              <td
                className={`p-2 ${
                  r.action === "BUY" ? "text-emerald-400" : "text-amber-400"
                }`}
              >
                {r.action}
              </td>
              <td className="p-2 text-right">{r.quantity}</td>
              <td className="p-2 text-right">${r.price.toFixed(2)}</td>
              <td className="p-2 text-right">${r.fees.toFixed(2)}</td>
              <td
                className={`p-2 text-right ${
                  r.realized_pnl == null
                    ? "text-zinc-500"
                    : r.realized_pnl >= 0
                      ? "text-emerald-400"
                      : "text-rose-400"
                }`}
              >
                {r.realized_pnl == null ? "—" : `$${r.realized_pnl.toFixed(2)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data?.next_cursor && (
        <div className="p-3 border-t border-zinc-800">
          <button
            className="text-xs text-violet-400"
            onClick={() => setCursor(data.next_cursor!)}
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
