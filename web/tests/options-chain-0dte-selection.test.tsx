// @vitest-environment jsdom

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import OptionsChainTab from "../components/ticker-detail/OptionsChainTab";
import type { PriceData } from "../lib/pricesProtocol";

vi.mock("../lib/TickerDetailContext", () => ({
  useTickerDetail: () => ({
    setChainContracts: vi.fn(),
  }),
}));

vi.mock("../lib/useChainPrefetch", () => ({
  useChainPrefetch: () => ({
    cacheStrikes: vi.fn(),
    getCachedStrikes: vi.fn(() => null),
  }),
}));

vi.mock("../lib/optionsChainUtils", async () => {
  const actual = await vi.importActual<typeof import("../lib/optionsChainUtils")>(
    "../lib/optionsChainUtils",
  );

  return {
    ...actual,
    daysToExpiry: (expiry: string) => {
      if (expiry === "20260422") return 0;
      if (expiry === "20260515") return 23;
      return actual.daysToExpiry(expiry);
    },
  };
});

const TICKER_PRICE: PriceData = {
  symbol: "AAPL",
  last: 205.5,
  lastIsCalculated: false,
  bid: 205.4,
  ask: 205.6,
  bidSize: 100,
  askSize: 100,
  volume: 1000,
  high: null,
  low: null,
  open: null,
  close: 204.2,
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

describe("OptionsChainTab 0DTE selection", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });

    fetchMock.mockImplementation((input) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : String(input.url);

      if (url.includes("/api/options/expirations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              symbol: "AAPL",
              expirations: ["20260422", "20260515"],
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }

      if (url.includes("/api/options/chain") && url.includes("expiry=20260515")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              symbol: "AAPL",
              expiry: "20260515",
              strikes: [210, 215, 220],
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }

      if (url.includes("/api/options/chain") && url.includes("expiry=20260422")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              symbol: "AAPL",
              expiry: "20260422",
              strikes: [95, 100, 105],
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }

      throw new Error(`Unexpected fetch: ${url}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps a dated expiry as default but loads the 0DTE chain when selected", async () => {
    const { container } = render(
      <OptionsChainTab
        ticker="AAPL"
        prices={{ AAPL: TICKER_PRICE }}
        tickerPriceData={TICKER_PRICE}
      />,
    );

    const expirySelect = await waitFor(() => {
      const select = container.querySelector<HTMLSelectElement>(
        ".chain-expiry-bar > select.chain-expiry-select",
      );
      expect(select).not.toBeNull();
      return select as HTMLSelectElement;
    });

    await waitFor(() => {
      expect(expirySelect.value).toBe("20260515");
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("expiry=20260515"),
      );
    });

    fireEvent.change(expirySelect, { target: { value: "20260422" } });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("expiry=20260422"),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("$100.00")).toBeTruthy();
    });
  });
});
