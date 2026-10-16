import { TelemetryLabel } from "@/components/atoms/TelemetryLabel";
import { SurfacePanelStack } from "@/components/organisms/SurfacePanelStack";
import { surfaceItems } from "@/lib/landing-content";

export function PreviewSection() {
  return (
    <section id="surfaces" className="py-16 md:py-24">
      <div className="max-w-3xl">
        <TelemetryLabel tone="core">Surface Preview</TelemetryLabel>
        <h2 className="mt-4 font-display text-4xl font-semibold text-primary md:text-5xl">
          Three surfaces. One coherent operating picture.
        </h2>
        <p className="mt-5 text-base leading-7 text-secondary">
          Portfolio, orders, and attribution read like related instruments inside
          the same terminal — one operating picture, not a set of disconnected
          dashboards.
        </p>
      </div>
      <div className="mt-8 grid gap-4 xl:grid-cols-3">
        {surfaceItems.map((item) => (
          <SurfacePanelStack key={item.name} item={item} />
        ))}
      </div>
    </section>
  );
}
