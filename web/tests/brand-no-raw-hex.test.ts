import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";
import postcss from "postcss";
import ts from "typescript";

/**
 * Brand guard (brand/CLAUDE.md: "All colors via tokens — no raw hex").
 *
 * Scans web/app, web/components and web/lib source (.ts/.tsx/.css) with real
 * parsers — postcss for CSS, the TypeScript AST for TS/TSX — and fails on
 * raw color literals (whole-token `#hex`, `rgb()/rgba()/hsl()/hsla()`)
 * outside:
 *
 *   - CSS custom-property declarations (`--name: value`) whose DIRECT
 *     parent rule is exactly `:root`, `[data-theme="dark"]` or
 *     `[data-theme="light"]` in web/app/globals.css — the approved token
 *     blocks;
 *   - JSX attribute values named `href`, `id`, `key`, `htmlFor` or `data-*`,
 *     and import/export module specifiers;
 *   - lines carrying a documented-exception comment `// brand:` or
 *     `/* brand:` (global-error literals that must survive globals.css not
 *     loading, devtools console styles that cannot resolve var()).
 */
const WEB = resolve(__dirname, "..");
const SCAN_DIRS = ["app", "components", "lib"];
const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);

const TOKEN_FILE = "app/globals.css";
const APPROVED_TOKEN_SELECTORS = new Set([
  ":root",
  '[data-theme="dark"]',
  '[data-theme="light"]',
]);

const BRAND_MARKER = /(\/\/|\/\*)\s*brand:/;

/** Whole-token hex (3, 4, 6 or 8 digits only) in a value string. */
const HEX_TOKEN =
  /(^|[\s(:,])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b(?=[\s),;:'"`!}]|$)/;
const COLOR_FN = /\b(?:rgba?|hsla?)\s*\(/;
const URL_REF = /url\([^)]*\)/g;

/**
 * Listed exceptions. A violation is skipped only when the file matches the
 * path AND the offending source line matches one of the patterns — anything
 * else in the same file still fails. Currently empty: surviving literals
 * carry a line-level `// brand:` / `/* brand:` marker instead (global-error,
 * perfTracker, .ratings-strong-sell background).
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

function markedOrExcepted(lines: string[], rel: string): boolean {
  const exceptions = LISTED_EXCEPTIONS[rel] ?? [];
  return lines.some(
    (l) => BRAND_MARKER.test(l) || exceptions.some((re) => re.test(l)),
  );
}

function scanCss(source: string, rel: string): string[] {
  const root = postcss.parse(source);
  const lines = source.split("\n");
  const hits: string[] = [];
  root.walkDecls((decl) => {
    const parent = decl.parent;
    const isTokenDecl =
      rel === TOKEN_FILE &&
      decl.prop.startsWith("--") &&
      parent?.type === "rule" &&
      APPROVED_TOKEN_SELECTORS.has((parent as postcss.Rule).selector.trim());
    if (isTokenDecl) return;
    if (!HEX_TOKEN.test(decl.value.replace(URL_REF, "url()")) &&
        !COLOR_FN.test(decl.value)) {
      return;
    }
    const start = (decl.source?.start?.line ?? 1) - 1;
    const end = (decl.source?.end?.line ?? start + 1) - 1;
    if (markedOrExcepted(lines.slice(start, end + 1), rel)) return;
    hits.push(`${rel}:${start + 1}: ${decl.toString().trim()}`);
  });
  return hits;
}

const FRAGMENT_ATTRS = new Set(["href", "id", "key", "htmlFor"]);

/**
 * Literal is the value of a JSX fragment/id attribute — a link, not a color.
 * Climbs ancestors (e.g. TemplateHead → TemplateExpression → JsxExpression →
 * JsxAttribute) so `href={`#deadbeef${id}`}` stays exempt.
 */
function isFragmentAttr(node: ts.Node): boolean {
  for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
    if (ts.isJsxAttribute(n)) {
      const name = n.name.text;
      return FRAGMENT_ATTRS.has(name) || name.startsWith("data-");
    }
  }
  return false;
}

function isModuleSpecifier(node: ts.Node): boolean {
  const p = node.parent;
  return (
    ts.isImportDeclaration(p) ||
    ts.isExportDeclaration(p) ||
    (ts.isCallExpression(p) &&
      p.expression.kind === ts.SyntaxKind.ImportKeyword)
  );
}

const LITERAL_KINDS = new Set([
  ts.SyntaxKind.StringLiteral,
  ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.TemplateHead,
  ts.SyntaxKind.TemplateMiddle,
  ts.SyntaxKind.TemplateTail,
]);

function scanTs(source: string, rel: string): string[] {
  const kind =
    rel.endsWith(".tsx") || rel.endsWith(".jsx")
      ? ts.ScriptKind.TSX
      : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(rel, source, ts.ScriptTarget.Latest, true, kind);
  const lines = source.split("\n");
  const hits: string[] = [];
  const visit = (node: ts.Node): void => {
    if (LITERAL_KINDS.has(node.kind)) {
      const text = (node as ts.StringLiteralLike).text;
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
      if (
        (HEX_TOKEN.test(text) || COLOR_FN.test(text)) &&
        !markedOrExcepted([lines[line] ?? ""], rel) &&
        !isFragmentAttr(node) &&
        !isModuleSpecifier(node)
      ) {
        hits.push(`${rel}:${line + 1}: ${(lines[line] ?? "").trim()}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

function violations(file: string): string[] {
  const source = readFileSync(file, "utf8");
  const rel = relative(WEB, file);
  return file.endsWith(".css") ? scanCss(source, rel) : scanTs(source, rel);
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

  // Resolved-value parity: each ratings fill/label pair must resolve to the
  // original color in BOTH themes.
  it("keeps the original ratings fill/label colors in both themes", () => {
    const root = postcss.parse(readFileSync(join(WEB, "app/globals.css"), "utf8"));
    const themeTokens: Record<string, Record<string, string>> = {
      ":root": {},
      '[data-theme="dark"]': {},
      '[data-theme="light"]': {},
    };
    root.walkRules((rule) => {
      const sel = rule.selector.trim();
      if (sel in themeTokens) {
        rule.walkDecls((d) => {
          if (d.prop.startsWith("--")) themeTokens[sel][d.prop] = d.value.trim();
        });
      }
    });
    const declValue = (selector: string, prop: string): string => {
      let out = "";
      root.walkRules(selector, (rule) => {
        rule.walkDecls(prop, (d) => {
          out = d.value;
        });
      });
      return out;
    };
    const resolveValue = (theme: string, value: string): string => {
      const seen = new Set<string>();
      const walk = (v: string): string =>
        v.replace(/var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)/g, (_, name: string) => {
          if (seen.has(name)) return `var(${name})`;
          seen.add(name);
          const t = themeTokens[theme][name] ?? themeTokens[":root"][name];
          return t === undefined ? `var(${name})` : walk(t);
        });
      return walk(value).toLowerCase();
    };
    const expected: Record<
      string,
      { dark: { bg: string; fg: string }; light: { bg: string; fg: string } }
    > = {
      "ratings-strong-buy": {
        dark: { bg: "#048a7a", fg: "#ffffff" },
        light: { bg: "#048a7a", fg: "#ffffff" },
      },
      "ratings-buy": {
        dark: { bg: "#05ad98", fg: "#000000" },
        light: { bg: "#048a7a", fg: "#000000" },
      },
      "ratings-hold": {
        dark: { bg: "#475569", fg: "#ffffff" },
        light: { bg: "#475569", fg: "#ffffff" },
      },
      "ratings-sell": {
        dark: { bg: "#e85d6c", fg: "#ffffff" },
        light: { bg: "#d4183d", fg: "#ffffff" },
      },
      "ratings-strong-sell": {
        dark: { bg: "#9f1239", fg: "#ffffff" },
        light: { bg: "#9f1239", fg: "#ffffff" },
      },
    };
    for (const [selector, themes] of Object.entries(expected)) {
      for (const [theme, want] of Object.entries(themes)) {
        expect(
          resolveValue(
            theme === "dark" ? '[data-theme="dark"]' : '[data-theme="light"]',
            declValue(`.${selector}`, "background"),
          ),
          `.${selector} background (${theme})`,
        ).toBe(want.bg);
        expect(
          resolveValue(
            theme === "dark" ? '[data-theme="dark"]' : '[data-theme="light"]',
            declValue(`.${selector}`, "color"),
          ),
          `.${selector} color (${theme})`,
        ).toBe(want.fg);
      }
    }
  });

  it("flags raw colors in TS/TSX literals but not fragments or specifiers", () => {
    const cases: [string, number][] = [
      ['const style = { border: "1px solid #fff" };', 1],
      ["const css = `0 0 2px #fff`;", 1],
      ['boxShadow: `0 0 2px #fff`,', 1],
      ['const c = "rgb(255,0,0)";', 1],
      ['const c = "rgba(0,0,0,0.4)";', 1],
      ['const c = "hsl(0, 0%, 50%)";', 1],
      ['const c = "#E85D6C";', 1],
      ['x: "var(--fault, #E85D6C)"', 1],
      // `}` is a valid hex terminator — the tail of a template expression
      ["const s = css`.x{width:${w}px;color:#fff}`;", 1],
      ['const c = "#abcd";', 1],
      ['const c = "#abcde";', 0],
      ['const c = "#abcdefg";', 0],
      ['const a = "/help#abc";', 0],
      ['<a href={"#deadbeef"} />', 0],
      ['<a href="#deadbeef">x</a>', 0],
      // fragment exemption reaches through template expressions
      ["<a href={`#deadbeef${id}`} />", 0],
      ['<div id="fff" />', 0],
      ['<div data-cell="#a1b2c3" />', 0],
      ['import x from "#a1b2c3";', 0],
      ['export { x } from "#a1b2c3";', 0],
      ['const path = "#fff-row";', 0],
    ];
    for (const [line, want] of cases) {
      expect(scanTs(`${line}\n`, "x.tsx"), line).toHaveLength(want);
    }
  });

  it("flags raw CSS colors only in declarations outside token blocks", () => {
    const mustFlag: string[] = [
      ".x { color:\n #fff;\n}\n",
      ".x { color: rgb(255,0,0); }\n",
      ".x { color: hsl(0, 0%, 50%); }\n",
      '.x { border: 1px solid #05ad98; }\n',
      '.x { color: #abcd; }\n',
      ':root { --a: #fff }\n.x { color: #fff; }\n',
      ':root { .card { --local: #fff; } }\n',
      '.zz { --local: var(--positive); color: #fff; }\n',
    ];
    for (const src of mustFlag) {
      expect(
        scanCss(src, "app/globals.css"),
        JSON.stringify(src),
      ).not.toHaveLength(0);
    }
    const clean: [string, string][] = [
      ['.x:hover #abc { display: block; }\n', "app/globals.css"],
      [':root {\n  --text-on-fill: #ffffff;\n}\n', "app/globals.css"],
      [':root {\n  --a: linear-gradient(180deg,\n    #fff,\n    #000);\n}\n', "app/globals.css"],
      ['[data-theme="dark"] {\n  --bg-base: #0a0f14;\n}\n', "app/globals.css"],
      ['[data-theme="light"] {\n  --bg-base: #ffffff;\n}\n', "app/globals.css"],
      ['.x { background: url("/img#f00d"); }\n', "app/globals.css"],
      // 5 hex digits is not a color length
      ['.x { color: #abcde; }\n', "app/globals.css"],
      // token defs are only legitimate in globals.css
      [':root {\n  --x: #fff;\n}\n', "app/other.css"],
    ];
    // token defs are only legitimate in globals.css — last case must flag
    expect(scanCss(clean[7][0], clean[7][1])).not.toHaveLength(0);
    for (const [src, rel] of clean.slice(0, 7)) {
      expect(scanCss(src, rel), JSON.stringify(src)).toEqual([]);
    }
  });
});
