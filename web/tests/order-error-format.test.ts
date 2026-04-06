import { describe, expect, it } from "vitest";
import { formatOrderError, formatOrderErrorMessage } from "../lib/orderError";

describe("formatOrderError", () => {
  it("strips transport wrappers and rewrites IB margin rejection to concise operator copy", () => {
    const raw = "Radon API 502: IB error 201: Order rejected - reason:YOUR ORDER IS NOT ACCEPTED. IN ORDER TO OBTAIN THE DESIRED POSITION YOUR PREVIOUS DAY EQUITY WITH LOAN VALUE <E> (644770.54 USD) MUST EXCEED THE INITIAL MARGIN (67243.00 USD).";

    expect(formatOrderError(raw)).toEqual({
      summary: "Order rejected by IB: insufficient margin.",
      details: [
        "Previous-day equity with loan value is $644,770.54; initial margin required is $67,243.00.",
      ],
    });
  });

  it("collapses generic IB cancellation text to a readable sentence", () => {
    expect(formatOrderError("Radon API 502: Order rejected by IB: Cancelled")).toEqual({
      summary: "Order rejected by IB.",
      details: ["Cancelled."],
    });
  });

  it("keeps the legacy string formatter aligned with the structured formatter", () => {
    expect(formatOrderErrorMessage("Radon API 502: Order rejected by IB: no acknowledgement (Unknown)")).toBe(
      "Order was not acknowledged by IB.",
    );
  });
});
