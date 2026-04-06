import { describe, expect, it } from "vitest";
import { getConnectionBannerState } from "../lib/ibConnectionAlert";

describe("getConnectionBannerState", () => {
  it("surfaces MFA approval guidance when the gateway reconnect requires operator action", () => {
    const banner = getConnectionBannerState({
      reconnected: false,
      wsConnected: true,
      ibConnected: false,
      ibIssue: "ibc_mfa_required",
      ibStatusMessage:
        "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.",
    });

    expect(banner).toEqual(expect.objectContaining({
      tone: "warning",
    }));
    expect(banner?.message).toMatch(/push notification/i);
    expect(banner?.message).toMatch(/phone/i);
  });

  it("does not surface an upper banner for generic websocket disconnects", () => {
    const banner = getConnectionBannerState({
      reconnected: false,
      wsConnected: false,
      ibConnected: false,
      ibIssue: null,
      ibStatusMessage: null,
    });

    expect(banner).toBeNull();
  });

  it("does not surface an upper banner for a generic reconnect success state", () => {
    const banner = getConnectionBannerState({
      reconnected: true,
      wsConnected: true,
      ibConnected: true,
      ibIssue: null,
      ibStatusMessage: null,
    });

    expect(banner).toBeNull();
  });
});
