import { isWriterStale } from "@/lib/serviceHealthWindows";
import type { WriterRow } from "@/lib/operatorTypes";
import { MarketState } from "@/lib/useMarketHours";

function ago(secs: number | null): string {
  if (secs == null) return "never";
  if (secs < 90) return `${secs}s ago`;
  if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function WriterFreshnessTable({
  writers,
  market,
}: {
  writers: WriterRow[];
  market: MarketState;
}) {
  return (
    <section className="snapshot-card operator-writers">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Writer Freshness</p>
        <h3 className="panel-title">Background writers</h3>
      </header>
      {writers.length === 0 ? (
        <p className="operator-writers__empty">No writers reported.</p>
      ) : (
        <table className="operator-writers__table">
          <thead>
            <tr>
              <th>Writer</th>
              <th>State</th>
              <th>Freshness</th>
              <th>Last run</th>
            </tr>
          </thead>
          <tbody>
            {writers.map((w) => {
              const stale = isWriterStale(w.service, w.age_secs, market);
              const fault = w.state === "error" || w.state === "missing";
              return (
                <tr key={w.service}>
                  <td className="operator-writers__name">{w.service}</td>
                  <td>
                    <span
                      className={`operator-pill operator-pill--${fault ? "fault" : "core"}`}
                    >
                      {w.state}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`operator-writers__fresh operator-writers__fresh--${
                        stale ? "stale" : "fresh"
                      }`}
                    >
                      {stale ? "STALE" : "fresh"}
                    </span>
                  </td>
                  <td className="operator-writers__age">{ago(w.age_secs)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
