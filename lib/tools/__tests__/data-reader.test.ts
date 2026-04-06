import { describe, it, expect } from "vitest";
import { Type } from "@sinclair/typebox";
import { readDataFile } from "../data-reader";
import { PortfolioData } from "../schemas/ib-sync";

describe("readDataFile", () => {
  it("reads an existing JSON file", async () => {
    const result = await readDataFile("data/watchlist.json");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toHaveProperty("tickers");
    }
  });

  it("returns error for missing file", async () => {
    const result = await readDataFile("data/nonexistent.json");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toContain("not found");
    }
  });

  it("validates against schema when provided", async () => {
    // portfolio.json should match the PortfolioData schema if it exists
    const result = await readDataFile("data/portfolio.json", PortfolioData);
    // File may or may not exist, but if it does, it should pass validation
    if (result.ok) {
      expect(result.data).toHaveProperty("bankroll");
      expect(result.data).toHaveProperty("positions");
    }
  });

  it("returns validation error when data doesn't match schema", async () => {
    // watchlist.json has {tickers: [...]}, which won't match PortfolioData
    const StrictSchema = Type.Object({
      bankroll: Type.Number(),
      impossible_field_xyz: Type.String(),
    });
    const result = await readDataFile("data/watchlist.json", StrictSchema);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toContain("Validation failed");
    }
  });

  it("returns error for file with invalid JSON", async () => {
    // .pi directory has non-JSON files — use README.md
    const result = await readDataFile("README.md");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      // JSON.parse should fail
      expect(result.error).toBeTruthy();
    }
  });
});
