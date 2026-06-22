// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import FxBadge from "@/components/FxBadge";

// Real usd_per_unit rates, 2026-06-22: JPY 1/161.6575=0.006186, KRW 1/1538.505=0.00065.
describe("FxBadge", () => {
  it("shows USD/JPY as JPY-per-USD (inverted from usd_per_unit)", () => {
    render(
      <FxBadge rates={{ USD: 1, JPY: 0.006186 }} liveCurrencies={["JPY"]} />,
    );
    expect(screen.getByText(/USD\/JPY/)).toBeTruthy();
    // 1 / 0.006186 ≈ 161.66
    expect(screen.getByText(/161/)).toBeTruthy();
  });

  it("marks a pair as snapshot (not live) when not in liveCurrencies", () => {
    const { container } = render(
      <FxBadge rates={{ USD: 1, KRW: 0.00065 }} liveCurrencies={[]} />,
    );
    expect(container.querySelector(".fx-dot-live")).toBeNull(); // hollow dot
    expect(screen.getByText(/USD\/KRW/)).toBeTruthy();
  });

  it("lights a live dot for a currency in liveCurrencies", () => {
    const { container } = render(
      <FxBadge rates={{ USD: 1, JPY: 0.006186 }} liveCurrencies={["JPY"]} />,
    );
    expect(container.querySelector(".fx-dot-live")).not.toBeNull();
  });

  it("renders nothing when only USD present", () => {
    const { container } = render(
      <FxBadge rates={{ USD: 1 }} liveCurrencies={[]} />,
    );
    expect(container.textContent).toBe("");
  });

  it("uses the standalone group class by default (keeps its bottom margin)", () => {
    const { container } = render(
      <FxBadge rates={{ USD: 1, JPY: 0.006186 }} liveCurrencies={["JPY"]} />,
    );
    const group = container.querySelector(".fx-badge-group");
    expect(group).not.toBeNull();
    expect(group!.classList.contains("fx-badge-group-inline")).toBe(false);
  });

  it("adds the inline modifier class when inline (for header placement)", () => {
    const { container } = render(
      <FxBadge
        rates={{ USD: 1, JPY: 0.006186 }}
        liveCurrencies={["JPY"]}
        inline
      />,
    );
    expect(container.querySelector(".fx-badge-group-inline")).not.toBeNull();
  });
});
