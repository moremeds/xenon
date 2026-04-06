import fs from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";

const SOURCE_PATH = path.resolve(__dirname, "../components/ticker-detail/OptionsChainTab.tsx");

describe("OptionsChainTab combo credit signing", () => {
  it("derives signedLimitPrice from combo debit/credit direction", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain("const signedLimitPrice =");
    expect(src).toMatch(/const signedLimitPrice = [\s\S]*Math\.abs\(parsedPrice\)\s*:\s*-Math\.abs\(parsedPrice\)/);
  });

  it("allows negative limit input for combo structures", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain('isValidPrice = !isNaN(parsedPrice) && (isCombo ? parsedPrice !== 0 : parsedPrice > 0)');
    expect(src).toContain('min={isCombo ? "-100000" : "0.01"}');
  });

  it("submits combo orders with signed limitPrice payload", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain("limitPrice: signedLimitPrice");
  });

  it("signs net quick-order values when rendering combo quote buttons", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain("signedNetPrices.bid");
    expect(src).toContain("signedNetPrices.mid");
    expect(src).toContain("signedNetPrices.ask");
    expect(src).toContain("signedNetPrices.bid.toFixed(2)");
    expect(src).toContain("signedNetPrices.mid.toFixed(2)");
    expect(src).toContain("signedNetPrices.ask.toFixed(2)");
  });

  it("uses credit/debit className for combo submit button", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain('className={`btn-primary ${isDebit === false ? "btn-danger" : ""}`}');
  });

  it("displays signed notional for combo orders", () => {
    const src = fs.readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain("signedLimitPrice * totalQty * 100");
    expect(src).toContain("const signedNetPrice = useCallback");
  });
});
