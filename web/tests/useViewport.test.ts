// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";
import { useViewport } from "@/lib/useViewport";

describe("useViewport", () => {
  it("returns hasMounted true after mount and an isMobile boolean", () => {
    const { result } = renderHook(() => useViewport());
    expect(typeof result.current.isMobile).toBe("boolean");
    expect(result.current.hasMounted).toBe(true);
  });
});
