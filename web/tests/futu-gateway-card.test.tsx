/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import { FutuGatewayCard } from "@/components/operator/FutuGatewayCard";
import type { FutuInfo, FutuQuotesInfo } from "@/lib/operatorTypes";

afterEach(() => cleanup());

const NO_HELD = {
  FUTU_FILL_UNPRICED: { count: 0, order_ids: [] },
  FUTU_FILL_CONFLICT: { count: 0, order_ids: [] },
  FUTU_NOT_OBSERVED: { count: 0, order_ids: [] },
};

const base: FutuInfo = {
  configured: true,
  connected: true,
  last_sync_at: "2026-06-15T13:59:00Z",
  last_sync_age_s: 42,
};

// Shape of the real prod payload (captured 2026-10-27, abbreviated).
const rich: FutuInfo = {
  configured: true,
  connected: false,
  last_sync_at: "2026-10-27T00:01:25.822Z",
  last_sync_age_s: 26488.4,
  observation: { as_of: "2026-10-27T07:22:13Z", complete: true },
  held_rows: NO_HELD,
  scope_env: "paper",
  trading: { paper: true, live: false },
  capabilities: {
    paper: {
      tuples: ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH", "STK:MKT:DAY:RTH"],
      modify: ["LMT"],
    },
    live: {
      tuples: [
        "OPT:LMT:DAY:RTH",
        "OPT:LMT:GTC:RTH",
        "STK:LMT:DAY:RTH",
        "STK:LMT:GTC:RTH",
      ],
      modify: ["LMT"],
    },
  },
  scopes: [
    {
      account_env: "paper",
      configured: true,
      last_sync_at: "2026-10-27T00:01:25.822Z",
      last_sync_age_s: 26488.7,
      observation: { as_of: "2026-10-27T07:22:13Z", complete: true },
      held_rows: NO_HELD,
    },
    {
      account_env: "live",
      configured: true,
      last_sync_at: "2026-10-27T00:01:05.648Z",
      last_sync_age_s: 26509.2,
      observation: { as_of: "2026-10-27T07:22:13Z", complete: false },
      held_rows: {
        ...NO_HELD,
        FUTU_NOT_OBSERVED: {
          count: 3,
          order_ids: [
            "FH1D55EEF9EE2D9000",
            "FH1D55EEFA738C0000",
            "FH1D55EEFAFB07D000",
          ],
        },
      },
    },
  ],
};

const quotes: FutuQuotesInfo = {
  connected: true,
  opend_connected: true,
  quota: { ceiling: 800, own_used: 24, all_used: 30, remain: 770 },
  routes: { futu: 12, ib: 3 },
};

const scopeRow = (env: string) =>
  document.querySelector(`[data-scope="${env}"]`) as HTMLElement;

describe("FutuGatewayCard", () => {
  it("no longer claims Futu is read-only or orders unsupported", () => {
    const { container } = render(<FutuGatewayCard futu={base} />);
    expect(screen.getByText("Futu")).toBeTruthy();
    expect(screen.getByText("sync")).toBeTruthy();
    expect(container.textContent).not.toMatch(/read-only|not supported/i);
    expect(container.querySelector(".operator-role--off")).toBeNull();
    expect(container.querySelector(".operator-role__dot--ok")).toBeTruthy();
  });

  it("renders an idle state when configured but disconnected", () => {
    const { container } = render(
      <FutuGatewayCard
        futu={{
          ...base,
          connected: false,
          last_sync_at: null,
          last_sync_age_s: null,
        }}
      />,
    );
    expect(screen.getByText(/idle/i)).toBeTruthy();
    expect(container.querySelector(".operator-role__dot--down")).toBeTruthy();
  });

  it("renders an off state when Futu is not configured", () => {
    render(
      <FutuGatewayCard
        futu={{ ...base, configured: false, connected: false }}
      />,
    );
    expect(screen.getByText(/^off$/i)).toBeTruthy();
  });

  it("renders per-scope rows with sync age, observation and trading state", () => {
    render(<FutuGatewayCard futu={rich} quotes={quotes} />);
    const paper = within(scopeRow("paper"));
    expect(paper.getByText("orders on")).toBeTruthy();
    expect(paper.getByText("7h")).toBeTruthy();
    expect(paper.getByText("observed")).toBeTruthy();
    expect(paper.getByText("held 0")).toBeTruthy();
    const live = within(scopeRow("live"));
    expect(live.getByText("orders off")).toBeTruthy();
    expect(live.getByText("incomplete")).toBeTruthy();
  });

  it("makes nonzero held rows a warn state and lists the order ids", () => {
    const { container } = render(
      <FutuGatewayCard futu={rich} quotes={quotes} />,
    );
    const live = within(scopeRow("live"));
    const pill = live.getByText("3 not observed");
    expect(pill.className).toContain("operator-pill--warn");
    expect(pill.getAttribute("title")).toContain("FH1D55EEFA738C0000");
    expect(live.getByText(/FH1D55EEF9EE2D9000/)).toBeTruthy();
    // Header verdict keeps its label but turns warn.
    const head = container.querySelector(
      ".snapshot-card__header .operator-pill",
    )!;
    expect(head.textContent).toBe("idle");
    expect(head.className).toContain("operator-pill--warn");
    // Clean scope stays quiet.
    expect(within(scopeRow("paper")).queryByText(/not observed/)).toBeNull();
  });

  it("keeps the neutral header when every held count is zero", () => {
    const { container } = render(
      <FutuGatewayCard futu={{ ...rich, scopes: [rich.scopes![0]] }} />,
    );
    const head = container.querySelector(
      ".snapshot-card__header .operator-pill",
    )!;
    expect(head.className).toContain("operator-pill--neutral");
  });

  it("tolerates null observation / held_rows / last_sync_at", () => {
    render(
      <FutuGatewayCard
        futu={{
          ...base,
          scopes: [
            {
              account_env: "paper",
              configured: true,
              last_sync_at: null,
              last_sync_age_s: null,
              observation: null,
              held_rows: null,
            },
          ],
        }}
      />,
    );
    const paper = within(scopeRow("paper"));
    expect(paper.getByText("obs ---")).toBeTruthy();
    expect(paper.getByText("held ---")).toBeTruthy();
  });

  it("marks a stored (rotated-out) scope instead of giving it the env's trading state", () => {
    const stored = { ...rich.scopes![0], configured: false };
    const { container } = render(
      <FutuGatewayCard futu={{ ...rich, scopes: [rich.scopes![0], stored] }} />,
    );
    const rows = container.querySelectorAll('[data-scope="paper"]');
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("orders on")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("stored")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).queryByText("orders on")).toBeNull();
  });

  it("shows the quotes line from realtime futu_quotes", () => {
    render(<FutuGatewayCard futu={rich} quotes={quotes} />);
    const line = screen.getByText(/OpenD up/);
    expect(line.textContent).toContain("futu 12");
    expect(line.textContent).toContain("ib 3");
    expect(line.textContent).toContain("24/800");
  });

  it("shows OpenD down and --- for missing quote fields", () => {
    const { rerender } = render(
      <FutuGatewayCard
        futu={rich}
        quotes={{
          connected: false,
          opend_connected: false,
          quota: null,
          routes: { futu: 0, ib: 5 },
        }}
      />,
    );
    expect(screen.getByText(/OpenD down/).textContent).toContain("quota ---");
    rerender(<FutuGatewayCard futu={rich} quotes={null} />);
    expect(screen.getByText("Quotes").nextElementSibling!.textContent).toBe(
      "---",
    );
    rerender(<FutuGatewayCard futu={rich} />);
    expect(screen.getByText("Quotes").nextElementSibling!.textContent).toBe(
      "---",
    );
  });

  it("keeps capabilities compact, full tuples in the title", () => {
    render(<FutuGatewayCard futu={rich} />);
    const caps = screen.getByText(/paper 3/);
    expect(caps.textContent).toContain("live 4");
    expect(caps.getAttribute("title")).toContain("STK:MKT:DAY:RTH");
  });
});
