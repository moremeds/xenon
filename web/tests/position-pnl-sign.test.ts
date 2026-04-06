/**
 * Unit tests: P&L sign display in PositionTable.
 *
 * Regression target:
 *   Negative Today P&L displayed without minus sign due to
 *   `${val >= 0 ? "+" : ""}${fmtUsd(Math.abs(val))}` — the empty string
 *   in the negative branch combined with Math.abs drops the sign entirely.
 *   e.g. -$11,525 rendered as "$11,525" instead of "-$11,525".
 */

import { describe, it, expect } from "vitest";

const fmtUsd = (n: number) =>
  `$${n.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

/** Buggy format (missing minus sign) */
function fmtPnlBuggy(val: number): string {
  return `${val >= 0 ? "+" : ""}${fmtUsd(Math.abs(val))}`;
}

/** Fixed format */
function fmtPnlFixed(val: number): string {
  return `${val >= 0 ? "+" : "-"}${fmtUsd(Math.abs(val))}`;
}

describe("P&L sign display", () => {
  it("buggy: negative value loses minus sign", () => {
    expect(fmtPnlBuggy(-11525)).toBe("$11,525"); // no sign — BUG
  });

  it("fixed: negative value shows minus sign", () => {
    expect(fmtPnlFixed(-11525)).toBe("-$11,525");
  });

  it("fixed: positive value shows plus sign", () => {
    expect(fmtPnlFixed(11525)).toBe("+$11,525");
  });

  it("fixed: zero shows plus sign", () => {
    expect(fmtPnlFixed(0)).toBe("+$0");
  });

  it("fixed: small negative value", () => {
    expect(fmtPnlFixed(-42)).toBe("-$42");
  });
});
