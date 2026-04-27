/**
 * @vitest-environment jsdom
 */

import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import VcgPanel from "../components/VcgPanel";

const mockUseVcg = vi.fn();

vi.mock("@/lib/useVcg", () => ({
  useVcg: (...args: unknown[]) => mockUseVcg(...args),
}));

function makeSignal() {
  return {
    vcg: 0.5,
    vcg_adj: 0.4,
    residual: 0.001,
    beta1_vvix: -0.01,
    beta2_vix: -0.02,
    alpha: 0,
    vix: 18.5,
    vvix: 96.2,
    credit_price: 79.5,
    credit_5d_return_pct: 0.12,
    ro: 0,
    edr: 0,
    tier: null,
    bounce: 0,
    vvix_severity: "moderate",
    sign_ok: true,
    sign_suppressed: false,
    pi_panic: 0,
    regime: "DIVERGENCE",
    interpretation: "NORMAL",
    attribution: {
      vvix_pct: 45,
      vix_pct: 55,
      vvix_component: 0,
      vix_component: 0,
      model_implied: 0,
    },
  };
}

describe("VcgPanel history ordering", () => {
  it("shows the latest business date first by default", () => {
    mockUseVcg.mockReturnValue({
      data: {
        scan_time: "2026-04-27T22:25:43Z",
        market_open: false,
        credit_proxy: "HYG",
        signal: makeSignal(),
        history: [
          {
            date: "2026-04-24",
            vcg: 0.19,
            vcg_adj: 0.18,
            residual: 0.001,
            beta1: -0.01,
            beta2: -0.02,
            vix: 18.71,
            vvix: 97.11,
            credit: 79.12,
          },
          {
            date: "2026-04-27",
            vcg: -0.02,
            vcg_adj: -0.02,
            residual: -0.001,
            beta1: -0.01,
            beta2: -0.02,
            vix: 18.76,
            vvix: 97.51,
            credit: 79.22,
          },
        ],
      },
      loading: false,
      error: null,
      lastSync: "2026-04-27T22:25:43Z",
    });

    const { container } = render(React.createElement(VcgPanel, { prices: {} }));

    const firstHistoryRow = container.querySelector("tbody tr");
    expect(firstHistoryRow?.querySelector("td")?.textContent).toBe("2026-04-27");
  });
});
