/**
 * @vitest-environment jsdom
 */

import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import Sidebar from "../components/Sidebar";

describe("Sidebar navigation", () => {
  it("hides nav items marked hidden while keeping the rest of the sidebar visible", () => {
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
    expect(screen.queryByRole("link", { name: /performance/i })).toBeNull();
  });
});
