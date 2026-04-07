/**
 * Static lookup from an options structure name (or `structure_type`) to
 * its catalog category. The catalog is loaded once at module import from
 * `docs/trading/options-structures.json` and frozen.
 *
 * The Portfolio "By Structure" view uses this to bucket positions under
 * an underlying into category sub-blocks (Single, Vertical, Covered, …).
 *
 * See `docs/superpowers/specs/2026-04-07-portfolio-by-structure-view-design.md`
 * for the design + review history.
 */

import rawCatalog from "../../docs/trading/options-structures.json";

export type CategoryKey =
  | "single"
  | "vertical"
  | "covered"
  | "collar"
  | "straddle"
  | "strangle"
  | "butterfly"
  | "condor"
  | "ratio"
  | "synthetic"
  | "horizontal"
  | "complex"
  | "other";

export const CATEGORY_ORDER: readonly CategoryKey[] = [
  "single",
  "vertical",
  "covered",
  "collar",
  "straddle",
  "strangle",
  "butterfly",
  "condor",
  "ratio",
  "synthetic",
  "horizontal",
  "complex",
  "other",
] as const;

export const CATEGORY_LABELS: Record<CategoryKey, string> = {
  single: "Single",
  vertical: "Vertical",
  covered: "Covered",
  collar: "Collar",
  straddle: "Straddle",
  strangle: "Strangle",
  butterfly: "Butterfly",
  condor: "Condor",
  ratio: "Ratio",
  synthetic: "Synthetic",
  horizontal: "Horizontal",
  complex: "Complex",
  other: "Other",
};

type CatalogEntry = {
  name: string;
  aliases?: string[];
  category: string;
};

const normalize = (s: string): string => s.trim().toLowerCase();

const LOOKUP: ReadonlyMap<string, CategoryKey> = (() => {
  const map = new Map<string, CategoryKey>();
  const entries = rawCatalog as CatalogEntry[];
  for (const entry of entries) {
    const category = entry.category as CategoryKey;
    if (!CATEGORY_ORDER.includes(category)) continue;
    const keys = [entry.name, ...(entry.aliases ?? [])];
    for (const key of keys) {
      map.set(normalize(key), category);
    }
  }
  return map;
})();

// Dev-only: warn once per unknown structure we're asked about so misses
// surface in development without spamming production logs.
const MISSED_WARNINGS = new Set<string>();

/** Test-only hook so warn-once behavior can be asserted deterministically. */
export function __resetMissWarningsForTests(): void {
  MISSED_WARNINGS.clear();
}

export function getStructureCategory(structure: string): CategoryKey {
  const key = normalize(structure);
  const hit = LOOKUP.get(key);
  if (hit) return hit;
  if (
    typeof process !== "undefined" &&
    process.env?.NODE_ENV !== "production" &&
    !MISSED_WARNINGS.has(key)
  ) {
    MISSED_WARNINGS.add(key);
    // eslint-disable-next-line no-console
    console.warn(
      `[structureCatalog] Unknown structure "${structure}" — falling back to "other"`,
    );
  }
  return "other";
}

/**
 * Returns the best catalog lookup key for a position. Prefers the
 * normalized `structure_type` field; falls back to `structure` with
 * strike/share-count decoration stripped (e.g. "Short Put $440.0" →
 * "Short Put", "Stock (10.8682 shares)" → "Stock").
 */
export function resolveStructureKey(pos: {
  structure_type?: string;
  structure?: string;
}): string {
  const st = (pos.structure_type ?? "").trim();
  if (st) return st;
  const raw = (pos.structure ?? "").trim();
  if (!raw) return "";
  // Strip "$<number>" strike suffix (with optional trailing fragments) and
  // "(…)" parenthetical decoration (e.g. share counts).
  return raw
    .replace(/\s*\$[\d.]+.*$/u, "")
    .replace(/\s*\([^)]*\)\s*$/u, "")
    .trim();
}
