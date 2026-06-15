/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { SignalTile } from "@/components/operator/SignalTile";

afterEach(() => cleanup());

describe("SignalTile", () => {
  it("renders label, value, sub", () => {
    render(
      <SignalTile label="Snapshotter" value="12s" sub="fresh" tone="core" />,
    );
    expect(screen.getByText("Snapshotter")).toBeTruthy();
    expect(screen.getByText("12s")).toBeTruthy();
    expect(screen.getByText("fresh")).toBeTruthy();
  });
  it("applies tone class", () => {
    const { container } = render(
      <SignalTile label="x" value="y" tone="fault" />,
    );
    expect(
      container.querySelector(".operator-tile__value--fault"),
    ).toBeTruthy();
  });
});
