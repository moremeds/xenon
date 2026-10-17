import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { runScript, resolveProjectRoot, _resetRootCache } from "../runner";

describe("resolveProjectRoot", () => {
  const originalCwd = process.cwd();

  beforeEach(() => _resetRootCache());
  afterEach(() => process.chdir(originalCwd));

  it("returns a path containing scripts/ and data/", () => {
    const root = resolveProjectRoot();
    expect(root).toBeTruthy();
    expect(existsSync(join(root, "scripts"))).toBe(true);
    expect(existsSync(join(root, "data"))).toBe(true);
  });

  it("caches the result", () => {
    const first = resolveProjectRoot();
    const second = resolveProjectRoot();
    expect(first).toBe(second);
  });

  it("does not depend on the process working directory", () => {
    process.chdir("/tmp");
    const root = resolveProjectRoot();
    expect(existsSync(join(root, "scripts"))).toBe(true);
    expect(existsSync(join(root, "data"))).toBe(true);
  });
});

// `.venv/bin/python` exercises the entry-point spawn branch directly —
// created by `uv sync` in CI and `uv run` locally, no PATH dependency.
const PY = ".venv/bin/python";
const KELLY_JSON =
  "import json; print(json.dumps({'full_kelly_pct': 10.0, 'edge_exists': True, 'recommendation': 'bet'}))";

describe("runScript", () => {
  it("returns ok: true with parsed JSON for a valid script", async () => {
    const result = await runScript(PY, {
      args: ["-c", KELLY_JSON],
      timeout: 10_000,
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toHaveProperty("full_kelly_pct");
      expect(result.data).toHaveProperty("edge_exists");
      expect(result.data).toHaveProperty("recommendation");
    }
  });

  it("returns ok: false for a non-existent script", async () => {
    const result = await runScript("scripts/nonexistent.py");

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.stderr).toContain("not found");
    }
  });

  it("returns ok: false when script exits non-zero", async () => {
    const result = await runScript(PY, {
      args: ["-c", "import sys; sys.exit(3)"],
      timeout: 10_000,
    });

    expect(result.ok).toBe(false);
  });

  it("supports rawOutput mode (no JSON parsing)", async () => {
    const result = await runScript(PY, {
      args: ["-c", KELLY_JSON],
      rawOutput: true,
      timeout: 10_000,
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      // rawOutput returns the string, not parsed JSON
      expect(typeof result.data).toBe("string");
      expect(result.data as string).toContain("full_kelly_pct");
    }
  });

  it("returns schema validation error when output doesn't match schema", async () => {
    const { Type } = await import("@sinclair/typebox");
    const WrongSchema = Type.Object({
      nonexistent_field: Type.String(),
    });

    const result = await runScript(PY, {
      args: ["-c", KELLY_JSON],
      outputSchema: WrongSchema,
      timeout: 10_000,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.stderr).toContain("Schema validation failed");
    }
  });
});
