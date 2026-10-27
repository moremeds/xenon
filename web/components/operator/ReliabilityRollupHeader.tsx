import type { IbAuthVerdict } from "@/lib/operatorTypes";

export function ReliabilityRollupHeader({
  verdict,
  updatedSecsAgo,
  writerSummary,
  degradedRoles = [],
}: {
  verdict: IbAuthVerdict;
  updatedSecsAgo: number | null;
  writerSummary: string;
  degradedRoles?: string[];
}) {
  const updated =
    updatedSecsAgo == null ? "updating…" : `updated ${updatedSecsAgo}s ago`;
  return (
    <div className="operator-rollup">
      <span className="operator-rollup__left">
        <span className="operator-rollup__summary">{writerSummary}</span>
        <span className="operator-rollup__sep">·</span>
        <span className="operator-rollup__verdict">
          IB {verdict}
          {degradedRoles.length > 0 ? (
            <span className="operator-rollup__degraded">
              {` · ${degradedRoles.join(", ")} down`}
            </span>
          ) : null}
        </span>
      </span>
      <span className="operator-rollup__updated">{updated}</span>
    </div>
  );
}
