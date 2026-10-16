import { SignalPill } from "@/components/atoms/SignalPill";
import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import { ExecutionStep } from "@/components/molecules/ExecutionStep";
import { executionItems } from "@/lib/landing-content";

export function ExecutionWorkflow() {
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1.35fr)_320px]">
      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {executionItems.map((item, index) => (
          <ExecutionStep key={item.step} item={item} index={index} />
        ))}
      </div>
      <aside className="border border-grid bg-panel px-5 py-5">
        <TelemetryLabel tone="core">Execution Guarantees</TelemetryLabel>
        <h3 className="mt-3 font-sans text-2xl font-semibold text-primary">
          No hidden handoff between order and book.
        </h3>
        <p className="mt-4 text-sm leading-6 text-secondary">
          Every position keeps its structure classification, every order carries
          its account scope, and every fill lands in Postgres. State stays
          inspectable end to end.
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <SignalPill tone="core">Guard Enforced</SignalPill>
          <SignalPill tone="strong">Account Scoped</SignalPill>
          <SignalPill tone="neutral">Postgres Backed</SignalPill>
        </div>
        <div className="mt-6 border-t border-grid pt-5">
          <TelemetryLabel>Operator Notes</TelemetryLabel>
          <ul className="mt-4 space-y-3 text-sm leading-6 text-secondary">
            <li>Every order carries broker, environment, and account scope.</li>
            <li>Coverage is checked before the broker sees the order.</li>
            <li>Short calls require long shares or same-expiry long calls; short puts are allowed.</li>
            <li>Uncovered exposure is blocked at the UI, the API, and the post-sync audit.</li>
          </ul>
        </div>
      </aside>
    </div>
  );
}
