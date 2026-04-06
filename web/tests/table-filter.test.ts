/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from "vitest";
import { useTableFilter } from "../lib/useTableFilter";
import { renderHook, act } from "@testing-library/react";

describe("useTableFilter", () => {
  const data = [
    { ticker: "AAPL", structure: "Bull Call Spread" },
    { ticker: "TSLA", structure: "Long Call" },
    { ticker: "IWM", structure: "Risk Reversal" },
    { ticker: "GOOG", structure: "Bull Call Spread" },
  ];

  const extract = (item: typeof data[number]) => `${item.ticker} ${item.structure}`;

  it("returns all data when query is empty", () => {
    const { result } = renderHook(() => useTableFilter(data, extract));
    expect(result.current.filtered).toHaveLength(4);
  });

  it("filters by ticker substring", () => {
    const { result } = renderHook(() => useTableFilter(data, extract));
    act(() => result.current.setQuery("TSLA"));
    expect(result.current.filtered).toHaveLength(1);
    expect(result.current.filtered[0].ticker).toBe("TSLA");
  });

  it("filters by structure substring (case-insensitive)", () => {
    const { result } = renderHook(() => useTableFilter(data, extract));
    act(() => result.current.setQuery("bull call"));
    expect(result.current.filtered).toHaveLength(2);
  });

  it("returns empty when no match", () => {
    const { result } = renderHook(() => useTableFilter(data, extract));
    act(() => result.current.setQuery("NVDA"));
    expect(result.current.filtered).toHaveLength(0);
  });

  it("clears filter", () => {
    const { result } = renderHook(() => useTableFilter(data, extract));
    act(() => result.current.setQuery("TSLA"));
    expect(result.current.filtered).toHaveLength(1);
    act(() => result.current.clear());
    expect(result.current.filtered).toHaveLength(4);
    expect(result.current.query).toBe("");
  });
});
