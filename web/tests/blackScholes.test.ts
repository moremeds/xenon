// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { bsCall, bsPut } from "@/lib/blackScholes";

describe("black-scholes", () => {
  it("ATM call ~ known value", () => {
    // S=100,K=100,T=1,r=0.05,sigma=0.2 → call ≈ 10.4506
    expect(bsCall(100, 100, 1, 0.05, 0.2)).toBeCloseTo(10.4506, 3);
  });
  it("ATM put ~ known value", () => {
    // put ≈ 5.5735 via put-call parity
    expect(bsPut(100, 100, 1, 0.05, 0.2)).toBeCloseTo(5.5735, 3);
  });
  it("intrinsic at T=0", () => {
    expect(bsCall(110, 100, 0, 0.05, 0.2)).toBeCloseTo(10, 6);
    expect(bsPut(90, 100, 0, 0.05, 0.2)).toBeCloseTo(10, 6);
  });
});
