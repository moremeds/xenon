import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import { ExecutionWorkflow } from "@/components/organisms/ExecutionWorkflow";

export function ExecutionSection() {
  return (
    <section id="execution" className="py-16 md:py-24">
      <div className="max-w-3xl">
        <TelemetryLabel tone="core">Execution Rail</TelemetryLabel>
        <h2 className="mt-4 font-display text-4xl font-semibold text-primary md:text-5xl">
          From snapshot to staged order to attributed fill. One path.
        </h2>
        <p className="mt-5 text-base leading-7 text-secondary">
          Xenon syncs account state, stages orders against live quotes, enforces
          coverage, routes to IB, and reconciles every fill back to the book.
        </p>
      </div>
      <div className="mt-8">
        <ExecutionWorkflow />
      </div>
    </section>
  );
}
