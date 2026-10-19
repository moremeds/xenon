/** @vitest-environment jsdom */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import WizardRiskAlertBanner from "@/components/ticker-detail/WizardRiskAlertBanner";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubAlerts(body: unknown, ok = true) {
  const fetchMock = vi.fn().mockResolvedValue({ ok, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const base = {
  threshold: "1.20",
  quote_as_of: "2026-10-20T15:00:00+00:00",
  evaluated_at: "2026-10-20T15:00:05+00:00",
};

describe("WizardRiskAlertBanner", () => {
  it("renders a CROSSED alert and a NO_QUOTE alert from /api/wizard/alerts", async () => {
    const fetchMock = stubAlerts({
      alerts: [
        { ...base, session_id: "s1", ticker: "AAPL", state: "CROSSED", quote: "1.10" },
        { ...base, session_id: "s2", ticker: "SPY", state: "NO_QUOTE", quote: null },
      ],
      events: [],
      monitor: { running: true, last_run_at: null },
    });
    render(<WizardRiskAlertBanner />);
    const crossed = await screen.findByTestId("wizard-risk-alert-s1");
    expect(fetchMock).toHaveBeenCalledWith("/api/wizard/alerts", { cache: "no-store" });
    expect(crossed.className).toContain("wizard-risk-alert--crossed");
    expect(crossed.textContent).toContain("AAPL net mid 1.10 crossed alert line 1.20");
    expect(crossed.textContent).toContain("Assisted Exit");
    const noQuote = screen.getByTestId("wizard-risk-alert-s2");
    expect(noQuote.className).toContain("wizard-risk-alert--no-quote");
    expect(noQuote.textContent).toContain("no fresh quote");
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("renders nothing when there are no alerts", async () => {
    const fetchMock = stubAlerts({ alerts: [], events: [], monitor: { running: true, last_run_at: null } });
    const { container } = render(<WizardRiskAlertBanner />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.innerHTML).toBe("");
  });

  it("renders nothing when the route fails", async () => {
    const fetchMock = stubAlerts({ detail: "down" }, false);
    const { container } = render(<WizardRiskAlertBanner />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.innerHTML).toBe("");
  });
});
