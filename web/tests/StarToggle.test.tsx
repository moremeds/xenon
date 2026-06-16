/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import StarToggle from "@/components/StarToggle";

afterEach(() => cleanup());

describe("StarToggle", () => {
  it("reflects active via aria-pressed and fires onToggle", () => {
    const onToggle = vi.fn();
    const { rerender } = render(
      <StarToggle active={false} onToggle={onToggle} />,
    );
    const btn = screen.getByRole("button");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledOnce();
    rerender(<StarToggle active onToggle={onToggle} />);
    expect(screen.getByRole("button").getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("does not fire when busy", () => {
    const onToggle = vi.fn();
    render(<StarToggle active={false} busy onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).not.toHaveBeenCalled();
  });
});
