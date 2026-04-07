// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

// Capture the config passed to useSyncHook so we can assert endpoint reactivity.
const seenConfigs: Array<{ endpoint: string }> = [];
vi.mock("@/lib/useSyncHook", () => ({
  useSyncHook: (config: { endpoint: string }) => {
    seenConfigs.push({ endpoint: config.endpoint });
    return {
      data: null, loading: false, syncing: false,
      error: null, lastSync: null, syncNow: () => {},
    };
  },
}));

import { useFlowAnalysis } from "@/lib/useFlowAnalysis";

describe("useFlowAnalysis", () => {
  beforeEach(() => {
    seenConfigs.length = 0;
  });

  it("requests the IB endpoint when account=ib", () => {
    renderHook(() => useFlowAnalysis("ib", true));
    expect(seenConfigs.at(-1)?.endpoint).toBe("/api/flow-analysis?account=ib");
  });

  it("requests the FUTU endpoint when account=futu", () => {
    renderHook(() => useFlowAnalysis("futu", true));
    expect(seenConfigs.at(-1)?.endpoint).toBe("/api/flow-analysis?account=futu");
  });

  it("re-issues a new endpoint when activeAccount changes", () => {
    const { rerender } = renderHook(
      ({ account }: { account: "ib" | "futu" }) => useFlowAnalysis(account, true),
      { initialProps: { account: "ib" } },
    );
    expect(seenConfigs.at(-1)?.endpoint).toBe("/api/flow-analysis?account=ib");

    rerender({ account: "futu" });
    expect(seenConfigs.at(-1)?.endpoint).toBe("/api/flow-analysis?account=futu");
  });
});
