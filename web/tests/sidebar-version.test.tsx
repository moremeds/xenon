/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, beforeEach } from "vitest";
import { render, cleanup } from "@testing-library/react";

import Sidebar from "@/components/Sidebar";

const ORIGINAL_VERSION = process.env.NEXT_PUBLIC_APP_VERSION;

afterEach(() => {
  cleanup();
  if (ORIGINAL_VERSION === undefined) {
    delete process.env.NEXT_PUBLIC_APP_VERSION;
  } else {
    process.env.NEXT_PUBLIC_APP_VERSION = ORIGINAL_VERSION;
  }
});

describe("Sidebar version row", () => {
  it("renders the app version from NEXT_PUBLIC_APP_VERSION with a v prefix", () => {
    process.env.NEXT_PUBLIC_APP_VERSION = "9.9.9";
    const { getByText } = render(
      <Sidebar
        activeSection="portfolio"
        actionTone="#22d3a6"
        ibConnected
        lastSync={null}
      />,
    );
    expect(getByText("Version")).toBeTruthy();
    expect(getByText("v9.9.9")).toBeTruthy();
  });

  it("falls back to em dash when version env is unset", () => {
    delete process.env.NEXT_PUBLIC_APP_VERSION;
    const { getByText, getAllByText } = render(
      <Sidebar
        activeSection="portfolio"
        actionTone="#22d3a6"
        ibConnected
        lastSync={null}
      />,
    );
    expect(getByText("Version")).toBeTruthy();
    // Last Sync (null) and Version both render the em dash fallback.
    expect(getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});
