import { CommandChip } from "@/components/atoms/CommandChip";
import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import { ProofMetric } from "@/components/molecules/ProofMetric";
import { HeroTerminalPanel } from "@/components/organisms/HeroTerminalPanel";
import { proofItems } from "@/lib/landing-content";

export function HeroSection() {
  return (
    <section id="top" className="relative py-14 md:py-20">
      <div className="grid gap-10 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] xl:items-start">
        <div>
          <TelemetryLabel tone="core">Broker Terminal</TelemetryLabel>
          <h1 className="mt-5 max-w-[12ch] font-display text-5xl font-semibold leading-[1.02] text-primary md:text-7xl">
            A broker terminal for options portfolio management.
          </h1>
          <p className="mt-6 max-w-2xl text-base leading-8 text-secondary md:text-lg">
            Xenon connects to Interactive Brokers for quotes, orders, and fills,
            with a read-only Futu position view. It enforces position-close
            rules, classifies structures, and attributes P&L and Greeks. No
            scanners, no signal feed — bring your own thesis.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <a
              href="#structures"
              className="inline-flex items-center border border-accent bg-accent px-5 py-3 font-mono text-[11px] uppercase tracking-[0.18em] text-canvas transition-colors hover:bg-signal-strong"
            >
              Inspect Structure Catalog
            </a>
            <a
              href="#execution"
              className="inline-flex items-center border border-grid bg-panel px-5 py-3 font-mono text-[11px] uppercase tracking-[0.18em] text-primary transition-colors hover:bg-panel-raised"
            >
              Review Execution Rail
            </a>
            <a
              href="https://github.com/joemccann/xenon"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex"
            >
              <CommandChip command="GitHub / Source" />
            </a>
          </div>
          <div className="mt-10 grid gap-4 md:grid-cols-2">
            {proofItems.map((item) => (
              <ProofMetric key={item.label} item={item} />
            ))}
          </div>
        </div>
        <HeroTerminalPanel />
      </div>
    </section>
  );
}
