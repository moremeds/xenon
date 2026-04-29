// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { SourcePill } from "../components/SourcePill";

describe("SourcePill", () => {
  it("renders nothing when source is missing or 'none'", () => {
    const { container } = render(<SourcePill source={undefined} />);
    expect(container.firstChild).toBeNull();
    const { container: c2 } = render(<SourcePill source="none" />);
    expect(c2.firstChild).toBeNull();
  });

  it.each([
    ["postgres", "PG"],
    ["flex", "FLEX"],
    ["postgres+flex", "PG+FLEX"],
  ] as const)("renders the %s pill", (source, label) => {
    render(<SourcePill source={source} />);
    expect(screen.getByText(label)).not.toBeNull();
  });
});
