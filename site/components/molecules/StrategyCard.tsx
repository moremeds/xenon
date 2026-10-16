import { CommandChip } from "@/components/atoms/CommandChip";
import { EdgeTrace } from "@/components/atoms/EdgeTrace";
import { SignalPill } from "@/components/atoms/SignalPill";
import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import type { StructureItem } from "@/lib/landing-content";

const decisionTone = {
  Allowed: "core" as const,
  Blocked: "warn" as const,
  "Guard Gap": "violet" as const,
};

export function StructureCard({
  structure,
}: {
  structure: StructureItem;
}) {
  const tone = structure.tone ?? "core";

  return (
    <article className="relative flex h-full flex-col border border-grid bg-panel px-5 py-5">
      <EdgeTrace tone={tone} />
      <div className="flex items-start justify-between gap-4">
        <div>
          <TelemetryLabel tone="core">Structure</TelemetryLabel>
          <h3 className="mt-3 font-sans text-xl font-semibold text-primary">
            {structure.name}
          </h3>
        </div>
        <SignalPill tone={decisionTone[structure.decision]}>
          {structure.decision}
        </SignalPill>
      </div>
      <p className="mt-4 text-sm leading-6 text-secondary">{structure.description}</p>
      <dl className="mt-6 grid gap-4 border-t border-grid pt-5 sm:grid-cols-2">
        <div>
          <TelemetryLabel>Legs</TelemetryLabel>
          <dd className="mt-2 text-sm leading-6 text-primary">{structure.legs}</dd>
        </div>
        <div>
          <TelemetryLabel>Class</TelemetryLabel>
          <dd className="mt-2 text-sm leading-6 text-primary">{structure.category}</dd>
        </div>
        <div>
          <TelemetryLabel>Guard</TelemetryLabel>
          <dd className="mt-2 font-mono text-[13px] uppercase tracking-[0.14em] text-secondary">
            {structure.guard}
          </dd>
        </div>
        <div>
          <TelemetryLabel>Bias</TelemetryLabel>
          <dd className="mt-2 font-mono text-[13px] uppercase tracking-[0.14em] text-secondary">
            {structure.bias}
          </dd>
        </div>
      </dl>
      <div className="mt-6 flex items-center justify-between gap-4 border-t border-grid pt-5">
        <TelemetryLabel tone="muted">Catalog Ref</TelemetryLabel>
        <CommandChip command={structure.slug} />
      </div>
    </article>
  );
}
