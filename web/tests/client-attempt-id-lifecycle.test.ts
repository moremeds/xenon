// @vitest-environment jsdom
import { describe, test, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useClientAttemptId } from "../components/ticker-detail/useClientAttemptId";

describe("useClientAttemptId lifecycle (SL §5.3 v0.2)", () => {
  test("seeds with a uuid on mount", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    expect(result.current.id).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("rotates on ticker change", () => {
    const { result, rerender } = renderHook(
      ({ ticker }) => useClientAttemptId({ ticker }),
      { initialProps: { ticker: "SPY" } },
    );
    const first = result.current.id;
    rerender({ ticker: "QQQ" });
    expect(result.current.id).not.toBe(first);
  });

  test("stable across in-flight retries", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.markSubmitted());
    expect(result.current.id).toBe(first);
  });

  test("rotates on qty edit after submit", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.markSubmitted());
    act(() => result.current.onFieldEdit("quantity"));
    expect(result.current.id).not.toBe(first);
  });

  test("does NOT rotate on field edits before any submit", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.onFieldEdit("quantity"));
    expect(result.current.id).toBe(first);
  });

  test("rotates on terminal response", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.markSubmitted());
    act(() => result.current.markTerminal());
    expect(result.current.id).not.toBe(first);
  });
});
