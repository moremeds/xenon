// @vitest-environment jsdom

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import OptionsChainTab from "../components/ticker-detail/OptionsChainTab";
import { TickerDetailProvider } from "../lib/TickerDetailContext";

describe("chain Implied column", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    if (!("scrollIntoView" in HTMLElement.prototype)) {
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
        configurable: true,
        value: vi.fn(),
      });
    } else {
      vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(
        () => {},
      );
    }

    fetchMock.mockImplementation((input) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : String((input as Request).url);
      if (url.includes("/api/options/expirations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ symbol: "SPX", expirations: ["20260116"] }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (url.includes("/api/options/chain")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              symbol: "SPX",
              expiry: "20260116",
              strikes: [4900, 5000, 5100],
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (url.includes("/api/previous-close")) {
        return Promise.resolve(
          new Response(JSON.stringify({ closes: { SPX: 5000 } }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders an Implied header for both sides", async () => {
    render(
      React.createElement(
        TickerDetailProvider,
        null,
        React.createElement(OptionsChainTab, {
          ticker: "SPX",
          prices: {},
          tickerPriceData: null,
        }),
      ),
    );
    // two "Implied" headers (calls + puts) once the chain renders
    await waitFor(() =>
      expect(screen.getAllByText("Implied").length).toBeGreaterThanOrEqual(1),
    );
    expect(screen.getAllByText("Implied").length).toBe(2);
  });
});
