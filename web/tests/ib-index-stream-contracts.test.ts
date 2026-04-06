import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = resolve(fileURLToPath(import.meta.url), "..");
const webDir = resolve(__dirname, "..");
const projectRoot = resolve(webDir, "..");
const source = readFileSync(resolve(projectRoot, "scripts", "ib_realtime_server.js"), "utf8");

describe("ib_realtime_server.js preserves typed contracts for cold-start restore", () => {
  it("seeds stock, option, and index subscriptions with their IB contract before the ibConnected gate", () => {
    expect(source).toContain("function ensureSymbolState");

    const stockBlock = source.match(/\/\/ Stock subscriptions[\s\S]*?\/\/ Option contract subscriptions/s)?.[0] ?? "";
    expect(stockBlock).toContain('const ibContract = ib.contract.stock(symbol, "SMART", "USD")');
    expect(stockBlock).toContain("ensureSymbolState(symbol, ibContract);");

    const optionBlock = source.match(/\/\/ Option contract subscriptions[\s\S]*?\/\/ Index subscriptions/s)?.[0] ?? "";
    expect(optionBlock).toContain("const ibContract = ib.contract.option(c.symbol, c.expiry, c.strike, c.right);");
    expect(optionBlock).toContain("ensureSymbolState(key, ibContract);");

    const indexBlock = source.match(/\/\/ Index subscriptions[\s\S]*?sendSubscribedConfirmation/s)?.[0] ?? "";
    expect(indexBlock).toContain('const ibContract = ib.contract.index(idx.symbol, "USD", idx.exchange);');
    expect(indexBlock).toContain("ensureSymbolState(key, ibContract);");
  });

  it("restores subscriptions from the stored contract instead of rebuilding everything as stocks", () => {
    const restoreBlock = source.match(/function restoreSubscriptions\(\) \{[\s\S]*?\n\}/)?.[0] ?? "";
    expect(restoreBlock).toContain("const ibContract = existing?.contract;");
    expect(restoreBlock).not.toContain('?? ib.contract.stock(key, "SMART", "USD")');
  });
});
