import { StructureCard } from "@/components/molecules/StrategyCard";
import { structures } from "@/lib/landing-content";

export function StrategyGrid() {
  return (
    <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
      {structures.map((structure) => (
        <StructureCard key={structure.name} structure={structure} />
      ))}
    </div>
  );
}
