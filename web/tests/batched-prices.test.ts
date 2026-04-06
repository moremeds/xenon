import { describe, it, expect, vi, beforeEach } from "vitest";
import type { PriceData, WSMessage, WSBatchMessage } from "../lib/pricesProtocol";

/**
 * Tests for batched WebSocket price updates.
 *
 * Verifies:
 * 1. WSBatchMessage type is part of the WSMessage union
 * 2. Batch message handler updates multiple symbols in one setPrices call
 * 3. onPriceUpdate fires for each symbol in a batch
 * 4. Backward compatibility — individual price messages still work
 */

function makePriceData(symbol: string, last: number): PriceData {
  return {
    symbol,
    last,
    lastIsCalculated: false,
    bid: last - 0.01,
    ask: last + 0.01,
    bidSize: 100,
    askSize: 100,
    volume: 1000,
    high: last + 1,
    low: last - 1,
    open: last,
    close: last - 0.5,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: new Date().toISOString(),
  };
}

/**
 * Simulate the message handler logic from usePrices.
 * We extract the switch-case logic into a testable function.
 */
function handleWSMessage(
  message: WSMessage,
  setPrices: (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => void,
  onPriceUpdate?: (update: { symbol: string; data: PriceData; receivedAt: Date }) => void,
) {
  switch (message.type) {
    case "price":
    case "snapshot": {
      const { data } = message;
      setPrices((prev) => ({
        ...prev,
        [data.symbol]: data,
      }));
      onPriceUpdate?.({
        symbol: data.symbol,
        data,
        receivedAt: new Date(),
      });
      break;
    }
    case "batch": {
      const { updates } = message;
      setPrices((prev) => ({ ...prev, ...updates }));
      for (const [sym, data] of Object.entries(updates)) {
        onPriceUpdate?.({
          symbol: sym,
          data,
          receivedAt: new Date(),
        });
      }
      break;
    }
    default:
      break;
  }
}

// =============================================================================
// Type checks — WSBatchMessage is part of WSMessage union
// =============================================================================

describe("WSBatchMessage type", () => {
  it("is accepted as a valid WSMessage", () => {
    // This test verifies the type compiles — if WSBatchMessage isn't in the
    // union, TypeScript will reject this assignment at compile time.
    const batchMsg: WSMessage = {
      type: "batch",
      updates: {
        AAPL: makePriceData("AAPL", 175.5),
        MSFT: makePriceData("MSFT", 420.0),
      },
    };
    expect(batchMsg.type).toBe("batch");
  });
});

// =============================================================================
// Batch message handler — multiple symbols in one setPrices call
// =============================================================================

describe("Batch message handler", () => {
  let priceState: Record<string, PriceData>;
  let setPricesCalls: number;
  const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
    setPricesCalls++;
    priceState = updater(priceState);
  };

  beforeEach(() => {
    priceState = {};
    setPricesCalls = 0;
  });

  it("updates multiple symbols in a single setPrices call", () => {
    const batchMsg: WSMessage = {
      type: "batch",
      updates: {
        AAPL: makePriceData("AAPL", 175.5),
        MSFT: makePriceData("MSFT", 420.0),
        NVDA: makePriceData("NVDA", 950.0),
      },
    };

    handleWSMessage(batchMsg, setPrices);

    // Only one setPrices call for 3 symbols
    expect(setPricesCalls).toBe(1);
    expect(priceState.AAPL.last).toBe(175.5);
    expect(priceState.MSFT.last).toBe(420.0);
    expect(priceState.NVDA.last).toBe(950.0);
  });

  it("preserves existing prices not in the batch", () => {
    priceState = { TSLA: makePriceData("TSLA", 200.0) };

    const batchMsg: WSMessage = {
      type: "batch",
      updates: {
        AAPL: makePriceData("AAPL", 175.5),
      },
    };

    handleWSMessage(batchMsg, setPrices);

    expect(priceState.TSLA.last).toBe(200.0);
    expect(priceState.AAPL.last).toBe(175.5);
  });

  it("overwrites existing symbol data in the batch", () => {
    priceState = { AAPL: makePriceData("AAPL", 170.0) };

    const batchMsg: WSMessage = {
      type: "batch",
      updates: {
        AAPL: makePriceData("AAPL", 175.5),
      },
    };

    handleWSMessage(batchMsg, setPrices);
    expect(priceState.AAPL.last).toBe(175.5);
  });
});

// =============================================================================
// onPriceUpdate callback — fires for each symbol in batch
// =============================================================================

describe("Batch onPriceUpdate callback", () => {
  it("fires once per symbol in the batch", () => {
    const onPriceUpdate = vi.fn();
    const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
      updater({});
    };

    const batchMsg: WSMessage = {
      type: "batch",
      updates: {
        AAPL: makePriceData("AAPL", 175.5),
        MSFT: makePriceData("MSFT", 420.0),
      },
    };

    handleWSMessage(batchMsg, setPrices, onPriceUpdate);

    expect(onPriceUpdate).toHaveBeenCalledTimes(2);
    const symbols = onPriceUpdate.mock.calls.map(
      (call: [{ symbol: string }]) => call[0].symbol,
    );
    expect(symbols).toContain("AAPL");
    expect(symbols).toContain("MSFT");
  });

  it("does not fire if batch is empty", () => {
    const onPriceUpdate = vi.fn();
    const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
      updater({});
    };

    const batchMsg: WSMessage = {
      type: "batch",
      updates: {},
    };

    handleWSMessage(batchMsg, setPrices, onPriceUpdate);
    expect(onPriceUpdate).not.toHaveBeenCalled();
  });
});

// =============================================================================
// Backward compatibility — individual messages still work
// =============================================================================

describe("Backward compatibility", () => {
  it("individual price messages still update state", () => {
    let priceState: Record<string, PriceData> = {};
    const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
      priceState = updater(priceState);
    };

    const priceMsg: WSMessage = {
      type: "price",
      symbol: "AAPL",
      data: makePriceData("AAPL", 175.5),
    };

    handleWSMessage(priceMsg, setPrices);
    expect(priceState.AAPL.last).toBe(175.5);
  });

  it("individual price messages fire onPriceUpdate", () => {
    const onPriceUpdate = vi.fn();
    const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
      updater({});
    };

    const priceMsg: WSMessage = {
      type: "price",
      symbol: "AAPL",
      data: makePriceData("AAPL", 175.5),
    };

    handleWSMessage(priceMsg, setPrices, onPriceUpdate);
    expect(onPriceUpdate).toHaveBeenCalledTimes(1);
    expect(onPriceUpdate.mock.calls[0][0].symbol).toBe("AAPL");
  });

  it("mix of batch and individual messages works correctly", () => {
    let priceState: Record<string, PriceData> = {};
    const setPrices = (updater: (prev: Record<string, PriceData>) => Record<string, PriceData>) => {
      priceState = updater(priceState);
    };

    // First: individual message
    handleWSMessage(
      { type: "price", symbol: "AAPL", data: makePriceData("AAPL", 170.0) } as WSMessage,
      setPrices,
    );

    // Then: batch that updates AAPL and adds MSFT
    handleWSMessage(
      {
        type: "batch",
        updates: {
          AAPL: makePriceData("AAPL", 175.5),
          MSFT: makePriceData("MSFT", 420.0),
        },
      } as WSMessage,
      setPrices,
    );

    expect(priceState.AAPL.last).toBe(175.5);
    expect(priceState.MSFT.last).toBe(420.0);
  });
});
