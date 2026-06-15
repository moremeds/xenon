/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DashboardSection } from "@/components/dashboard/DashboardSection";

afterEach(() => cleanup());

describe("DashboardSection", () => {
  it("renders label, count, and expanded body by default", () => {
    render(
      <DashboardSection id="portfolio" label="Portfolio" count="01">
        <p>child content</p>
      </DashboardSection>,
    );
    expect(screen.getByText("Portfolio")).toBeTruthy();
    expect(screen.getByText("01")).toBeTruthy();
    const toggle = screen.getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("child content")).toBeTruthy();
  });

  it("collapses and re-expands the body on toggle", () => {
    render(
      <DashboardSection id="orders" label="Working & Filled">
        <p>child content</p>
      </DashboardSection>,
    );
    const toggle = screen.getByRole("button");
    const body = document.getElementById("dashboard-section-body-orders")!;
    expect(body.hasAttribute("hidden")).toBe(false);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(body.hasAttribute("hidden")).toBe(true);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(body.hasAttribute("hidden")).toBe(false);
  });
});
