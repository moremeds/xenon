import type { BlotterSource } from "../lib/types";

const LABELS: Record<Exclude<BlotterSource, "none">, string> = {
  postgres: "PG",
  flex: "FLEX",
  "postgres+flex": "PG+FLEX",
};

export function SourcePill({ source }: { source?: BlotterSource }) {
  if (!source || source === "none") return null;
  return (
    <span className="pill neutral" data-testid="source-pill">
      {LABELS[source]}
    </span>
  );
}
