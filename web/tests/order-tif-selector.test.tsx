/**
 * @vitest-environment jsdom
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OrderTifSelector } from "@/lib/order/components/OrderTifSelector";

afterEach(() => cleanup());

describe("OrderTifSelector", () => {
  it("marks the selected GTC button as visibly pressed", () => {
    const onChange = vi.fn();
    const { getByRole } = render(<OrderTifSelector tif="GTC" onChange={onChange} />);

    const day = getByRole("button", { name: "DAY" });
    const gtc = getByRole("button", { name: "GTC" });

    expect(day.getAttribute("aria-pressed")).toBe("false");
    expect(gtc.getAttribute("aria-pressed")).toBe("true");
    expect(gtc.classList.contains("order-tif-active")).toBe(true);

    fireEvent.click(day);
    expect(onChange).toHaveBeenCalledWith("DAY");
  });
});
