/**
 * @vitest-environment jsdom
 */

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ShieldBadge } from "@/components/portfolio/ShieldBadge";

afterEach(() => cleanup());

describe("ShieldBadge", () => {
  it("uses signal token for ARMED", () => {
    const { getByRole } = render(<ShieldBadge state="ARMED" />);
    expect(getByRole("button").getAttribute("data-state")).toBe("ARMED");
    expect(getByRole("button").getAttribute("data-tone")).toContain("--signal-core");
  });

  it("uses warn token for PENDING_ARM", () => {
    const { getByRole } = render(<ShieldBadge state="PENDING_ARM" />);
    expect(getByRole("button").getAttribute("data-tone")).toContain("--warning");
  });

  it("uses fault token for FAILED", () => {
    const { getByRole } = render(<ShieldBadge state="FAILED" />);
    expect(getByRole("button").getAttribute("data-tone")).toContain("--fault");
  });

  it("renders a count when supplied", () => {
    const { getByRole } = render(<ShieldBadge state="ARMED" count={2} />);
    expect(getByRole("button").textContent).toContain("2");
  });

  it("uses neutral token for UNCLASSIFIED", () => {
    const { getByRole } = render(<ShieldBadge state="UNCLASSIFIED" />);
    expect(getByRole("button").getAttribute("data-tone")).toContain("--neutral");
  });
});
