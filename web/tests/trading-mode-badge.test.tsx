/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, waitFor } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

import TradingModeBadge from "@/components/TradingModeBadge";

function stubFetch(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status: ok ? 200 : 500,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

describe("TradingModeBadge", () => {
  it("renders PAPER MODE capsule when /api/health returns paper", async () => {
    stubFetch({ trading_mode: "paper" });
    const { getByText } = render(<TradingModeBadge />);
    await waitFor(() => {
      expect(getByText(/PAPER MODE/i)).toBeTruthy();
    });
  });

  it("renders nothing for live mode", async () => {
    stubFetch({ trading_mode: "live" });
    const { container } = render(<TradingModeBadge />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(container.querySelector(".trading-mode-badge")).toBeNull();
  });

  it("renders nothing when /api/health is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network"))),
    );
    const { container } = render(<TradingModeBadge />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(container.querySelector(".trading-mode-badge")).toBeNull();
  });
});
