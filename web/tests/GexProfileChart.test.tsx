// @vitest-environment jsdom
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";

import GexProfileChart from "@/components/charts/GexProfileChart";
import type { GexBucket } from "@/lib/useGex";

afterEach(() => cleanup());

const BUCKETS: GexBucket[] = [
  {
    strike: 200,
    call_gex: 1_200_000,
    put_gex: -100_000,
    net_gex: 1_100_000,
    pct_from_spot: 2.5,
    tag: "CALL WALL",
  },
  {
    strike: 195,
    call_gex: 0,
    put_gex: 0,
    net_gex: 0,
    pct_from_spot: 0,
    tag: "SPOT",
  },
  {
    strike: 190,
    call_gex: 100_000,
    put_gex: -900_000,
    net_gex: -800_000,
    pct_from_spot: -2.5,
    tag: "PUT WALL",
  },
];

describe("GexProfileChart", () => {
  it("renders an SVG with one <rect> bar per bucket", () => {
    const { container } = render(
      <GexProfileChart profile={BUCKETS} spot={195} />,
    );
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    const rects = container.querySelectorAll("svg rect");
    expect(rects.length).toBe(BUCKETS.length);
    // Strike labels should be present.
    expect(container.textContent).toContain("200");
    expect(container.textContent).toContain("190");
    // Tag text for SPOT and CALL WALL should be rendered.
    expect(container.textContent).toContain("SPOT");
    expect(container.textContent).toContain("CALL WALL");
  });

  it("renders without crashing when profile is empty", () => {
    const { container } = render(<GexProfileChart profile={[]} spot={100} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    // No bar rects when there are no buckets.
    expect(container.querySelectorAll("svg rect").length).toBe(0);
    // Title still renders.
    expect(container.textContent).toContain("GEX Profile");
  });
});
