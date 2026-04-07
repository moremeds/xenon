import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  getStructureCategory,
  resolveStructureKey,
  CATEGORY_ORDER,
  CATEGORY_LABELS,
  __resetMissWarningsForTests,
} from "@/lib/structureCatalog";

describe("structureCatalog", () => {
  describe("getStructureCategory", () => {
    it("maps canonical catalog names to their category", () => {
      expect(getStructureCategory("Bull Call Spread")).toBe("vertical");
      expect(getStructureCategory("Long Call")).toBe("single");
      expect(getStructureCategory("Iron Condor")).toBe("condor");
      expect(getStructureCategory("Covered Call")).toBe("covered");
    });

    it("maps aliases to their category", () => {
      // "Protective Put" is an alias of "Long Put" (single).
      // Previously also aliased to "Covered Put" — collision resolved in JSON.
      expect(getStructureCategory("Protective Put")).toBe("single");
    });

    it("maps real IB structure_type strings after catalog alias extension", () => {
      // These are the bare structure_type values that live IB data produces.
      expect(getStructureCategory("Long Call")).toBe("single");
      expect(getStructureCategory("Short Call")).toBe("single");
      expect(getStructureCategory("Short Put")).toBe("single");
    });

    it("is case and whitespace insensitive", () => {
      expect(getStructureCategory("  short put  ")).toBe("single");
      expect(getStructureCategory("BULL CALL SPREAD")).toBe("vertical");
      expect(getStructureCategory("bull CALL spread")).toBe("vertical");
    });

    it("returns 'other' for unknown structures without throwing", () => {
      expect(getStructureCategory("Random Exotic Thing")).toBe("other");
      expect(getStructureCategory("")).toBe("other");
    });

    it("warns once per unique unknown key in dev", () => {
      __resetMissWarningsForTests();
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
      try {
        getStructureCategory("Unknown Widget");
        getStructureCategory("Unknown Widget"); // repeat — should not warn again
        getStructureCategory("Another Unknown"); // different key — should warn
        expect(warnSpy).toHaveBeenCalledTimes(2);
      } finally {
        warnSpy.mockRestore();
      }
    });
  });

  describe("CATEGORY_ORDER", () => {
    it("has 13 entries in the expected order", () => {
      expect(CATEGORY_ORDER).toEqual([
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
      ]);
    });

    it("every category has a label", () => {
      for (const key of CATEGORY_ORDER) {
        expect(CATEGORY_LABELS[key]).toBeTruthy();
      }
    });
  });

  describe("resolveStructureKey", () => {
    it("prefers structure_type when present", () => {
      expect(
        resolveStructureKey({
          structure_type: "Short Put",
          structure: "Short Put $440.0",
        }),
      ).toBe("Short Put");
    });

    it("falls back to structure stripped of strike decoration when structure_type is empty", () => {
      expect(
        resolveStructureKey({
          structure_type: "",
          structure: "Short Put $440.0",
        }),
      ).toBe("Short Put");
    });

    it("falls back to structure stripped of strike decoration when structure_type is undefined", () => {
      expect(
        resolveStructureKey({
          structure: "Long Call $250.0",
        }),
      ).toBe("Long Call");
    });

    it("leaves undecorated structure as-is in the fallback path", () => {
      expect(
        resolveStructureKey({
          structure_type: "",
          structure: "Bull Call Spread",
        }),
      ).toBe("Bull Call Spread");
    });

    it("strips Stock-style share-count decoration", () => {
      expect(
        resolveStructureKey({
          structure_type: "Stock",
          structure: "Stock (10.8682 shares)",
        }),
      ).toBe("Stock");
    });

    it("returns empty string when both fields are missing", () => {
      expect(resolveStructureKey({})).toBe("");
    });
  });
});
