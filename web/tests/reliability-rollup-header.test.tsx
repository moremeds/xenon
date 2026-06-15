/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ReliabilityRollupHeader } from "@/components/operator/ReliabilityRollupHeader";

afterEach(() => cleanup());

describe("ReliabilityRollupHeader", () => {
  it("renders verdict + updated age + writer summary", () => {
    render(
      <ReliabilityRollupHeader
        verdict="authenticated"
        updatedSecsAgo={2}
        writerSummary="3 healthy"
      />,
    );
    expect(screen.getByText(/authenticated/i)).toBeTruthy();
    expect(screen.getByText(/updated 2s ago/i)).toBeTruthy();
    expect(screen.getByText(/3 healthy/i)).toBeTruthy();
  });
  it("shows an updating state when age is null", () => {
    render(
      <ReliabilityRollupHeader
        verdict="unknown"
        updatedSecsAgo={null}
        writerSummary="0/0 healthy"
      />,
    );
    expect(screen.getByText(/updating/i)).toBeTruthy();
  });
});
