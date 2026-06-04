// @vitest-environment jsdom
import { describe, test, expect, beforeEach, afterEach } from "vitest";
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

// Browsers gate crypto.randomUUID behind "secure context" — HTTPS, localhost,
// 127.0.0.1. Plain-HTTP LAN access (e.g. http://hostname:3000 over Tailscale
// MagicDNS) is non-secure, so crypto.randomUUID is undefined and throws when
// invoked. The hook must still seed/rotate an id-shaped string in that case.
describe("useClientAttemptId in non-secure browser context", () => {
  let original: PropertyDescriptor | undefined;

  beforeEach(() => {
    original = Object.getOwnPropertyDescriptor(globalThis.crypto, "randomUUID");
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      value: undefined,
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    if (original) {
      Object.defineProperty(globalThis.crypto, "randomUUID", original);
    }
  });

  test("seeds with a uuid-shaped id when crypto.randomUUID is unavailable", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    expect(result.current.id).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("rotates on ticker change without crypto.randomUUID", () => {
    const { result, rerender } = renderHook(
      ({ ticker }) => useClientAttemptId({ ticker }),
      { initialProps: { ticker: "SPY" } },
    );
    const first = result.current.id;
    rerender({ ticker: "QQQ" });
    expect(result.current.id).not.toBe(first);
    expect(result.current.id).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("produces unique ids across many mounts", () => {
    const ids = new Set<string>();
    for (let i = 0; i < 50; i += 1) {
      const { result } = renderHook(() =>
        useClientAttemptId({ ticker: "SPY" }),
      );
      ids.add(result.current.id);
    }
    expect(ids.size).toBe(50);
  });
});
