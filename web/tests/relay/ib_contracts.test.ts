import { describe, it, expect } from "vitest";
import {
  stockContract,
  optionContract,
  indexContract,
  futureContract,
} from "../../../scripts/infra/ib_realtime/ib_contracts.js";
import { SecType } from "@stoqey/ib";

describe("ib_contracts", () => {
  it("builds a SMART stock contract", () => {
    expect(stockContract("AAPL")).toEqual({
      symbol: "AAPL",
      secType: SecType.STK,
      exchange: "SMART",
      currency: "USD",
    });
  });

  it("builds an option contract (SMART/USD, OCC fields)", () => {
    expect(optionContract("AAPL", "20260116", 200, "C")).toEqual({
      symbol: "AAPL",
      secType: SecType.OPT,
      exchange: "SMART",
      currency: "USD",
      lastTradeDateOrContractMonth: "20260116",
      strike: 200,
      right: "C",
      multiplier: "100",
    });
  });

  it("builds an index contract on its exchange", () => {
    expect(indexContract("SPX", "CBOE")).toEqual({
      symbol: "SPX",
      secType: SecType.IND,
      exchange: "CBOE",
      currency: "USD",
    });
  });

  it("builds a future contract on its native exchange (forward-compat)", () => {
    expect(futureContract("ES", "20260320", "CME")).toEqual({
      symbol: "ES",
      secType: SecType.FUT,
      exchange: "CME",
      currency: "USD",
      lastTradeDateOrContractMonth: "20260320",
    });
  });
});
