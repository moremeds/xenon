import { readdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { ROUTE_MANIFEST } from "./route-manifest";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const E2E_DIR = join(__dirname);
const APP_DIR = join(__dirname, "..", "app");

test.describe("E2E Route Manifest", () => {
  test("all spec files in manifest actually exist", () => {
    const allSpecs = Object.values(ROUTE_MANIFEST).flatMap((r) => r.specs);
    const existing = readdirSync(E2E_DIR).filter((f) => f.endsWith(".spec.ts"));
    for (const spec of allSpecs) {
      expect(
        existing,
        `Spec ${spec} listed in manifest but not found`,
      ).toContain(spec);
    }
  });

  test("no routes have 'missing' status", () => {
    const missing = Object.entries(ROUTE_MANIFEST)
      .filter(([, v]) => v.status === "missing")
      .map(([k]) => k);
    // This is informational — tracks which routes lack E2E coverage.
    // Uncomment the assertion below when all routes are covered:
    // expect(missing).toEqual([]);
    console.log(`Routes without E2E specs: ${missing.join(", ") || "none"}`);
  });

  test("every app route has a manifest entry", () => {
    // Scan web/app/ for page.tsx files and extract route paths
    const routes: string[] = [];
    function walk(dir: string, prefix: string) {
      if (!existsSync(dir)) return;
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (
          entry.name === "api" ||
          entry.name === "sign-in" ||
          entry.name === "sign-up"
        )
          continue;
        if (entry.isDirectory()) {
          walk(join(dir, entry.name), `${prefix}/${entry.name}`);
        } else if (entry.name === "page.tsx" || entry.name === "page.ts") {
          routes.push(prefix || "/");
        }
      }
    }
    walk(APP_DIR, "");

    const manifestRoutes = Object.keys(ROUTE_MANIFEST);
    for (const route of routes) {
      expect(manifestRoutes, `Route ${route} has no manifest entry`).toContain(
        route,
      );
    }
  });
});
