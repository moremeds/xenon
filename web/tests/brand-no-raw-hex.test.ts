import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";

/**
 * Brand guard (brand/CLAUDE.md: "All colors via tokens — no raw hex").
 *
 * Scans web/app, web/components and web/lib source (.ts/.tsx/.css) and fails
 * on any raw `#hex` or `rgb()/rgba()` literal outside:
 *
 *   - CSS custom-property definitions: `--name: <value>;` (multi-line values
 *     are covered — everything up to the terminating `;` is the definition);
 *   - lines carrying a documented-exception comment `// brand:` or
 *     `/* brand:` (e.g. global-error literals that must survive globals.css
 *     not loading, devtools console styles that cannot resolve var()).
 */
const WEB = resolve(__dirname, "..");
const SCAN_DIRS = ["app", "components", "lib"];
const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);

const RAW_COLOR = /#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(/;
const BRAND_MARKER = /(\/\/|\/\*)\s*brand:/;
const VAR_DEF_START = /^\s*--[\w-]+\s*:/;

/**
 * Listed exceptions. A violation is skipped only when the file matches the
 * path AND the line matches one of the patterns — anything else in the same
 * file still fails. Currently empty: surviving literals carry a line-level
 * `// brand:` / `/* brand:` marker instead (global-error, perfTracker,
 * .ratings-strong-sell background).
 */
const LISTED_EXCEPTIONS: Record<string, RegExp[]> = {};

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const abs = join(dir, entry);
    const st = statSync(abs);
    if (st.isDirectory()) {
      if (entry === "node_modules" || entry === ".next") continue;
      yield* sourceFiles(abs);
    } else if (EXTENSIONS.has(extname(entry))) {
      yield abs;
    }
  }
}

function violations(file: string): string[] {
  const isCss = file.endsWith(".css");
  const hits: string[] = [];
  let inVarDef = false;
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((line, i) => {
      if (!inVarDef && isCss && VAR_DEF_START.test(line)) inVarDef = true;
      const excepted = (LISTED_EXCEPTIONS[relative(WEB, file)] ?? []).some(
        (re) => re.test(line),
      );
      if (!inVarDef && !excepted && !BRAND_MARKER.test(line) && RAW_COLOR.test(line)) {
        hits.push(`${relative(WEB, file)}:${i + 1}: ${line.trim()}`);
      }
      if (inVarDef && line.includes(";")) inVarDef = false;
    });
  return hits;
}

describe("brand tokens", () => {
  it("has no raw hex/rgb color literals outside token definitions", () => {
    const found = SCAN_DIRS.flatMap((dir) =>
      [...sourceFiles(join(WEB, dir))].flatMap(violations),
    );
    expect(found).toEqual([]);
  });

  // Regression: --accent-text is theme-dependent (#0a0f14 dark / #ffffff
  // light); labels on saturated fills must use the theme-independent
  // --text-on-fill / --text-on-fill-dark tokens to keep the original contrast.
  it("uses theme-independent on-fill label tokens on saturated fills", () => {
    const css = readFileSync(join(WEB, "app/globals.css"), "utf8");
    const expected: Record<string, string> = {
      "connection-banner": "--text-on-fill",
      "btn-danger": "--text-on-fill",
      "ratings-strong-buy": "--text-on-fill",
      "ratings-buy": "--text-on-fill-dark",
      "ratings-hold": "--text-on-fill",
      "ratings-sell": "--text-on-fill",
    };
    for (const [selector, token] of Object.entries(expected)) {
      const block = css.match(new RegExp(`\\.${selector}\\s*\\{[^}]*\\}`));
      expect(block?.[0] ?? "", `.${selector}`).toContain(
        `color: var(${token})`,
      );
    }
  });
});
