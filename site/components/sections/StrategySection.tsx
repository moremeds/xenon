import { SignalPill } from "@/components/atoms/SignalPill";
import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import { StrategyGrid } from "@/components/organisms/StrategyGrid";

export function StrategySection() {
  return (
    <section id="structures" className="py-16 md:py-24">
      <div className="flex flex-col gap-6 border-b border-grid pb-8 md:flex-row md:items-end md:justify-between">
        <div className="max-w-3xl">
          <TelemetryLabel tone="core">Structure Catalog</TelemetryLabel>
          <h2 className="mt-4 font-display text-4xl font-semibold text-primary md:text-5xl">
            Every recognized structure carries an explicit guard decision.
          </h2>
          <p className="mt-5 text-base leading-7 text-secondary">
            The catalog classifies what the terminal can stage: legs, category,
            guard outcome, and bias are visible per structure, so coverage rules
            stay inspectable before an order reaches the broker.
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <SignalPill tone="core">41 Allowed</SignalPill>
          <SignalPill tone="warn">14 Blocked</SignalPill>
          <SignalPill tone="violet">3 Guard Gaps</SignalPill>
        </div>
      </div>
      <div className="mt-8">
        <StrategyGrid />
      </div>
    </section>
  );
}
