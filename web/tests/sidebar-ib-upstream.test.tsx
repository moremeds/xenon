// @vitest-environment jsdom
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import Sidebar from "../components/Sidebar";
import { parseIbUpstream } from "../lib/ibUpstream";

afterEach(cleanup);

const base = { activeSection: "portfolio" as const, actionTone: "#fff" };

describe("IB upstream state (incident 2026-10-26)", () => {
  it("parses ib_gateway.upstream from /api/health", () => {
    expect(
      parseIbUpstream({
        ib_gateway: {
          upstream: {
            state: "lost",
            since: "2026-10-26T04:13:00+00:00",
            last_code: 1100,
          },
        },
      }),
    ).toEqual({
      state: "lost",
      since: "2026-10-26T04:13:00+00:00",
      lastCode: 1100,
    });
    expect(parseIbUpstream({ ib_gateway: {} }).state).toBe("unknown");
    expect(parseIbUpstream(null).state).toBe("unknown");
    expect(
      parseIbUpstream({ ib_gateway: { upstream: { state: "weird" } } }).state,
    ).toBe("unknown");
  });

  it("shows NO IB SERVERS with a warning dot when the socket is up but upstream is lost", () => {
    const { container } = render(
      <Sidebar
        {...base}
        ibConnected
        ibUpstream={{
          state: "lost",
          since: "2026-10-26T04:13:00+00:00",
          lastCode: 2110,
        }}
      />,
    );
    expect(screen.getByText("NO IB SERVERS")).toBeTruthy();
    expect(screen.getByTestId("ib-upstream-lost").textContent).toContain(
      "(2110)",
    );
    expect(container.querySelector(".status-dot-stale")).toBeTruthy();
    expect(screen.queryByText("CONNECTED")).toBeNull();
  });

  it("stays CONNECTED when upstream is connected or unknown", () => {
    render(
      <Sidebar
        {...base}
        ibConnected
        ibUpstream={{ state: "connected", since: null, lastCode: 1102 }}
      />,
    );
    expect(screen.getByText("CONNECTED")).toBeTruthy();
    expect(screen.queryByTestId("ib-upstream-lost")).toBeNull();
  });

  it("OFFLINE wins over upstream lost", () => {
    render(
      <Sidebar
        {...base}
        ibConnected={false}
        ibUpstream={{ state: "lost", since: null, lastCode: 1100 }}
      />,
    );
    expect(screen.getByText("OFFLINE")).toBeTruthy();
    expect(screen.queryByTestId("ib-upstream-lost")).toBeNull();
  });
});
