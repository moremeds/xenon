/**
 * @vitest-environment jsdom
 */

import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import Sidebar from "../components/Sidebar";

describe("Sidebar navigation", () => {
  it("renders the portfolio-terminal nav set", () => {
    render(
      createElement(Sidebar, {
        activeSection: "portfolio",
        actionTone: "#05AD98",
        ibConnected: false,
        lastSync: null,
      }),
    );

    expect(screen.getByRole("link", { name: /dashboard/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /portfolio/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /performance/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /orders/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /journal/i })).toBeTruthy();
    // Signal nav items were removed in the pure-portfolio pivot.
    expect(screen.queryByRole("link", { name: /scanner/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /regime/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /flow analysis/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /uw analysis/i })).toBeNull();
  });
});
