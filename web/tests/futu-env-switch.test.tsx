/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import FutuEnvSwitch, { FutuAccountEnvBadge } from "@/components/FutuEnvSwitch";
import { getFutuAccountEnv, setFutuAccountEnv } from "@/lib/futuAccountEnv";

const store = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  value: {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => store.set(k, v),
    removeItem: (k: string) => store.delete(k),
    clear: () => store.clear(),
  },
  configurable: true,
});

beforeEach(() => {
  store.clear();
  setFutuAccountEnv("paper");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FutuEnvSwitch", () => {
  it("defaults to SIMULATE and toggles to REAL", () => {
    render(<FutuEnvSwitch />);
    expect(
      screen.getByTestId("futu-env-simulate").getAttribute("aria-pressed"),
    ).toBe("true");
    fireEvent.click(screen.getByTestId("futu-env-real"));
    expect(getFutuAccountEnv()).toBe("live");
    expect(
      screen.getByTestId("futu-env-real").getAttribute("aria-pressed"),
    ).toBe("true");
  });
});

describe("FutuAccountEnvBadge", () => {
  it("shows SIMULATE with neutral token and REAL with warning token", () => {
    const { rerender } = render(<FutuAccountEnvBadge env="paper" />);
    const badge = screen.getByTestId("futu-env-badge");
    expect(badge.textContent).toBe("SIMULATE");
    expect(badge.style.color).toBe("var(--neutral)");
    rerender(<FutuAccountEnvBadge env="live" />);
    expect(screen.getByTestId("futu-env-badge").textContent).toBe("REAL");
    expect(screen.getByTestId("futu-env-badge").style.color).toBe(
      "var(--warning)",
    );
  });
});
