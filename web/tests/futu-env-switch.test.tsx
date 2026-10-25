/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import FutuEnvSwitch from "@/components/FutuEnvSwitch";
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

describe("FutuEnvSwitch onSelect", () => {
  it("reports the picked env and marks REAL with the warning token", () => {
    const onSelect = vi.fn();
    render(<FutuEnvSwitch onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("futu-env-real"));
    expect(onSelect).toHaveBeenCalledWith("live");
    expect(screen.getByTestId("futu-env-real").style.color).toBe(
      "var(--warning)",
    );
  });
});
