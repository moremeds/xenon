/**
 * @vitest-environment jsdom
 *
 * Regression for the "modify a combo order = cancel + place" data-loss bug.
 *
 * BEFORE: ModifyOrderModal always emitted `{ replaceOrder: ... }` for any combo
 * modify. The Next.js modify route then cancels the existing order before
 * placing a replacement. If the place call failed (validation, IB-201, server
 * error), the user was left with a cancelled order and no replacement.
 *
 * AFTER: For price/qty-only modifies on a combo, ModifyOrderModal emits the
 * standard `{ newPrice, newQuantity }` request, which routes through FastAPI's
 * `modify_order` (atomic IB modify — re-uses the same orderId). The
 * cancel+place path is only used when the leg structure actually changed.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import ModifyOrderModal from "@/components/ModifyOrderModal";
import type { OpenOrder } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

afterEach(() => cleanup());

function comboOrder(): OpenOrder {
  return {
    orderId: 12345,
    permId: 9876543210,
    symbol: "SPY spread",
    action: "SELL",
    orderType: "LMT",
    tif: "DAY",
    totalQuantity: 4,
    limitPrice: 2.5,
    auxPrice: null,
    contract: {
      symbol: "SPY",
      secType: "BAG",
      strike: null,
      right: null,
      expiry: null,
      comboLegs: [
        {
          action: "BUY",
          symbol: "SPY",
          expiry: "20260619",
          strike: 200,
          right: "C",
          ratio: 1,
        },
        {
          action: "SELL",
          symbol: "SPY",
          expiry: "20260619",
          strike: 210,
          right: "C",
          ratio: 1,
        },
      ],
    },
  } as unknown as OpenOrder;
}

describe("ModifyOrderModal — combo modify routing", () => {
  it("price-only change emits ModifyOrderRequest with newPrice (atomic modify), NOT replaceOrder", async () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={comboOrder()}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    // Change only the limit price (legs untouched).
    const priceInput = getByLabelText(
      /New Net Price|New Limit Price/i,
    ) as HTMLInputElement;
    fireEvent.change(priceInput, { target: { value: "2.75" } });

    fireEvent.click(getByRole("button", { name: /Modify Order/i }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
    const req = onConfirm.mock.calls[0][0];

    expect(req.replaceOrder).toBeUndefined(); // critical — no cancel+place
    expect(req.newPrice).toBeCloseTo(2.75, 2);
    expect(req.newQuantity).toBeUndefined(); // qty unchanged
  });

  it("qty-only change emits ModifyOrderRequest with newQuantity, NOT replaceOrder", async () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={comboOrder()}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    const qtyInput = getByLabelText(/New Quantity/i) as HTMLInputElement;
    fireEvent.change(qtyInput, { target: { value: "8" } });

    fireEvent.click(getByRole("button", { name: /Modify Order/i }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
    const req = onConfirm.mock.calls[0][0];

    expect(req.replaceOrder).toBeUndefined();
    expect(req.newQuantity).toBe(8);
  });

  it("price + qty change still atomic (no replaceOrder)", async () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={comboOrder()}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    fireEvent.change(
      getByLabelText(/New Net Price|New Limit Price/i) as HTMLInputElement,
      { target: { value: "2.75" } },
    );
    fireEvent.change(getByLabelText(/New Quantity/i) as HTMLInputElement, {
      target: { value: "8" },
    });

    fireEvent.click(getByRole("button", { name: /Modify Order/i }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
    const req = onConfirm.mock.calls[0][0];

    expect(req.replaceOrder).toBeUndefined();
    expect(req.newPrice).toBeCloseTo(2.75, 2);
    expect(req.newQuantity).toBe(8);
  });

  it("leg-structure change DOES use replaceOrder (cancel+place is required for restructuring)", async () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={comboOrder()}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    // Edit a leg strike — that's a real restructure, only doable via cancel+place.
    const strikeInput = getByLabelText(/Strike/i, {
      selector: "#modify-leg-1-strike",
    }) as HTMLInputElement;
    fireEvent.change(strikeInput, { target: { value: "215" } });

    // Also tweak price so canSubmit is satisfied via priceChanged or legsChanged.
    fireEvent.change(
      getByLabelText(/New Net Price|New Limit Price/i) as HTMLInputElement,
      { target: { value: "2.40" } },
    );

    fireEvent.click(getByRole("button", { name: /Modify Order/i }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
    const req = onConfirm.mock.calls[0][0];

    expect(req.replaceOrder).toBeDefined();
    expect(req.replaceOrder.type).toBe("combo");
    expect(req.replaceOrder.legs).toHaveLength(2);
  });
});
