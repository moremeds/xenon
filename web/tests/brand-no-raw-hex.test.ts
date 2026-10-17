import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";

/**
 * Brand guard (brand/CLAUDE.md: "All colors via tokens — no raw hex").
 *
 * Scans web/app, web/components and web/lib source (.ts/.tsx/.css) and fails
 * on raw `#hex` / `rgb()` / `rgba()` / `hsl()` literals in color-bearing
 * syntax — CSS declaration values and JS/TS strings used as style values —
 * while ignoring:
 *
 *   - `--name: value;` token definitions, but only inside the approved
 *     token blocks (`:root`, `[data-theme="dark"]`, `[data-theme="light"]`)
 *     of web/app/globals.css, and only the declaration span;
 *   - fragment/id refs such as `href="#deadbeef"` or `id="#fff-row"`;
 *   - lines carrying a documented-exception comment `// brand:` or
 *     `/* brand:` (global-error literals that must survive globals.css not
 *     loading, devtools console styles that cannot resolve var()).
 */
const WEB = resolve(__dirname, "..");
const SCAN_DIRS = ["app", "components", "lib"];
const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);

const BRAND_MARKER = /(\/\/|\/\*)\s*brand:/;
const VAR_DEF = /^\s*--[\w-]+\s*:/;

/** Token definitions are only legitimate inside these globals.css blocks. */
const TOKEN_FILE = "app/globals.css";
const TOKEN_BLOCK_OPEN = /^\s*(?::root|\[data-theme="(?:dark|light)"\])\s*\{/;

const HEX = /#[0-9a-fA-F]{3,8}\b/g;
const COLOR_FN = /\brgba?\s*\(|\bhsla?\s*\(/;
const URL_REF = /url\([^)]*\)/g;
/** `attr="#…"` fragment/id refs are links, not colors. */
const TS_FRAGMENT_ATTR =
  /\b(?:href|to|id|rel|name|anchor|fragment)\s*=\s*["'`][^"'`]*["'`]/g;
/** hex in a color-value position: quote-adjacent or after `:`, `,`, `(`. */
const TS_HEX_VALUE = /["'`:(,]\s*#[0-9a-fA-F]{3,8}(?=["'`);,\s]|$)/;

/** CSS: a literal counts only inside a declaration value (after the colon). */
function cssHasRawColor(text: string): boolean {
  const t = text.replace(URL_REF, "url()");
  if (COLOR_FN.test(t)) return true;
  const colon = t.indexOf(":");
  if (colon === -1) return false;
  return [...t.matchAll(HEX)].some((m) => (m.index ?? 0) > colon);
}

/** TS/TSX: a literal counts in a style string / color call, not fragments. */
function tsHasRawColor(text: string): boolean {
  const t = text.replace(TS_FRAGMENT_ATTR, '""');
  return COLOR_FN.test(t) || TS_HEX_VALUE.test(t);
}

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

function scanSource(
  source: string,
  opts: { isCss: boolean; rel: string },
): string[] {
  const hits: string[] = [];
  const isTokenFile = opts.rel === TOKEN_FILE;
  let inTokenBlock = false;
  let inVarDef = false;
  source.split("\n").forEach((rawLine, i) => {
    let text = rawLine;
    if (opts.isCss) {
      if (inVarDef) {
        const end = text.indexOf(";");
        if (end === -1) return; // inside a multi-line token value
        inVarDef = false;
        text = text.slice(end + 1); // scan the rest of the line
      }
      if (!inTokenBlock && isTokenFile && TOKEN_BLOCK_OPEN.test(text)) {
        inTokenBlock = true;
        text = text.slice(text.indexOf("{") + 1);
      }
      // `--name: value;` defs inside an approved block are exempt — but only
      // the declaration span; the remainder of the line is still scanned.
      while (inTokenBlock && VAR_DEF.test(text)) {
        const end = text.indexOf(";");
        if (end === -1) {
          inVarDef = true;
          return;
        }
        text = text.slice(end + 1);
      }
      if (inTokenBlock && text.includes("}")) inTokenBlock = false;
    }
    const excepted = (LISTED_EXCEPTIONS[opts.rel] ?? []).some((re) =>
      re.test(rawLine),
    );
    if (excepted || BRAND_MARKER.test(rawLine)) return;
    const dirty = opts.isCss ? cssHasRawColor(text) : tsHasRawColor(text);
    if (dirty) hits.push(`${opts.rel}:${i + 1}: ${rawLine.trim()}`);
  });
  return hits;
}

function violations(file: string): string[] {
  return scanSource(readFileSync(file, "utf8"), {
    isCss: file.endsWith(".css"),
    rel: relative(WEB, file),
  });
}

describe("brand tokens", () => {
  it("has no raw hex/rgb color literals outside token definitions", () => {
    const found = SCAN_DIRS.flatMap((dir) =>
      [...sourceFiles(join(WEB, dir))].flatMap(violations),
    );
    expect(found).toEqual([]);
  });

  // Regression: --accent-text and --text-muted are theme-dependent; labels
  // and fills on saturated backgrounds must use theme-independent tokens to
  // keep the original colors in both themes.
  it("uses theme-independent tokens for filled ratings/label pairs", () => {
    const css = readFileSync(join(WEB, "app/globals.css"), "utf8");
    const expected: Record<string, { label: string; fill?: string }> = {
      "connection-banner": { label: "--text-on-fill" },
      "btn-danger": { label: "--text-on-fill" },
      "ratings-strong-buy": {
        label: "--text-on-fill",
        fill: "--rating-strong-buy-fill",
      },
      "ratings-buy": { label: "--text-on-fill-dark" },
      "ratings-hold": { label: "--text-on-fill", fill: "--rating-hold-fill" },
      "ratings-sell": { label: "--text-on-fill" },
      "ratings-strong-sell": { label: "--text-on-fill" },
    };
    for (const [selector, { label, fill }] of Object.entries(expected)) {
      const block = css.match(new RegExp(`\\.${selector}\\s*\\{[^}]*\\}`));
      expect(block?.[0] ?? "", `.${selector}`).toContain(`color: var(${label})`);
      if (fill) {
        expect(block?.[0] ?? "", `.${selector}`).toContain(
          `background: var(${fill})`,
        );
      }
    }
  });

  it("flags raw colors only in color-bearing syntax", () => {
    const tsCases: [string, number][] = [
      ['const a = "/help#abc";', 0],
      ['<a href="#deadbeef">x</a>', 0],
      ['<div id="#fff-row" />', 0],
      ['const c = "#E85D6C";', 1],
      ['style={{ color: "#fff" }}', 1],
      ['bg: "rgba(0,0,0,0.4)"', 1],
      ['x: "var(--fault, #E85D6C)"', 1],
      ['t: "hsl(0, 0%, 50%)"', 1],
    ];
    for (const [line, want] of tsCases) {
      expect(
        scanSource(`${line}\n`, { isCss: false, rel: "x.tsx" }),
        line,
      ).toHaveLength(want);
    }
    const cssCases: [string, number][] = [
      ['.x#a1b2c3:hover { content: ""; }', 0],
      ['background: url("/img#f00d");', 0],
      ["color: #e85d6c;", 1],
      ["border: 1px solid #05ad98;", 1],
      ["background: rgba(0, 0, 0, 0.4);", 1],
    ];
    for (const [line, want] of cssCases) {
      expect(
        scanSource(`${line}\n`, { isCss: true, rel: "app/x.css" }),
        line,
      ).toHaveLength(want);
    }
  });

  it("exempts token definitions only in approved globals.css blocks", () => {
    const exempt: string[] = [
      ":root {\n  --text-on-fill: #ffffff;\n}\n",
      ":root {\n  --a: linear-gradient(180deg,\n    #fff,\n    #000);\n}\n",
      '[data-theme="dark"] {\n  --bg-base: #0a0f14;\n}\n',
      '[data-theme="light"] {\n  --bg-base: #ffffff;\n}\n',
      ":root {\n  --a: #fff; --b: #000;\n}\n",
    ];
    for (const src of exempt) {
      expect(
        scanSource(src, { isCss: true, rel: "app/globals.css" }),
        src,
      ).toEqual([]);
    }
    const flagged: [string, string][] = [
      // a declaration sharing the line after a token def must still be scanned
      [":root {\n  --local: var(--positive); color: #fff;\n}\n", "app/globals.css"],
      // defs outside the approved blocks are not exempt
      [".card {\n  --accent: #fff;\n}\n", "app/globals.css"],
      // token defs are only allowed in globals.css
      [":root {\n  --x: #fff;\n}\n", "app/other.css"],
    ];
    for (const [src, rel] of flagged) {
      expect(scanSource(src, { isCss: true, rel }), src).toHaveLength(1);
    }
  });
});
