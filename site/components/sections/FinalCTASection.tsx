import { CommandChip } from "@/components/atoms/CommandChip";
import { SignalPill } from "@/components/atoms/SignalPill";
import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";

export function FinalCTASection() {
  return (
    <section className="py-16 md:py-24">
      <div className="border border-grid bg-panel px-6 py-8 md:px-8 md:py-10">
        <TelemetryLabel tone="core">Operator Handoff</TelemetryLabel>
        <div className="mt-5 grid gap-8 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-end">
          <div>
            <h2 className="max-w-[16ch] font-display text-4xl font-semibold leading-tight text-primary md:text-5xl">
              Inspect the terminal before the next position forces the question.
            </h2>
            <p className="mt-5 max-w-2xl text-base leading-7 text-secondary">
              Xenon is for operators who want execution and accounting to stay
              inspectable. Read the source, check the guard, then decide.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <SignalPill tone="core">Guarded</SignalPill>
              <SignalPill tone="strong">Account Scoped</SignalPill>
              <SignalPill tone="neutral">Open Source</SignalPill>
            </div>
          </div>
          <div className="flex flex-wrap gap-3 xl:justify-end">
            <a
              href="#structures"
              className="inline-flex items-center border border-accent bg-accent px-5 py-3 font-mono text-[11px] uppercase tracking-[0.18em] text-canvas transition-colors hover:bg-signal-strong"
            >
              Review the Catalog
            </a>
            <a
              href="https://github.com/joemccann/xenon"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex"
            >
              <CommandChip command="Inspect Source" />
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
