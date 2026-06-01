import { describe, expect, it } from "vitest";
import {
  IB_MFA_REQUIRED_ISSUE,
  classifyIBConnectionError,
} from "../../scripts/infra/ib_realtime/ib_connection_status.js";

describe("classifyIBConnectionError", () => {
  it("treats a local gateway ECONNREFUSED as an MFA approval issue", () => {
    const issue = classifyIBConnectionError(
      "connect ECONNREFUSED 127.0.0.1:4001",
      {
        ibHost: "127.0.0.1",
        ibPort: 4001,
      },
    );

    expect(issue).toEqual(
      expect.objectContaining({
        code: IB_MFA_REQUIRED_ISSUE,
      }),
    );
    expect(issue?.operatorMessage).toMatch(/Interactive Brokers/i);
    expect(issue?.operatorMessage).toMatch(/push notification/i);
    expect(issue?.operatorMessage).toMatch(/phone/i);
  });

  it("classifies ECONNREFUSED reported against the resolved IP, not the configured hostname (docker-bridge case)", () => {
    // Regression: Node's `net` emits the resolved IP (e.g. 192.168.5.2) in
    // the error text, but the relay is configured with
    // IB_GATEWAY_HOST=host.docker.internal. Matching the error against the
    // configured target silently misses, which kills the relay's reconnect
    // loop. The classifier must catch this.
    const issue = classifyIBConnectionError(
      "connect ECONNREFUSED 192.168.5.2:4001",
      {
        ibHost: "host.docker.internal",
        ibPort: 4001,
      },
    );

    expect(issue?.code).toBe(IB_MFA_REQUIRED_ISSUE);
  });

  it.each([
    "connect ETIMEDOUT 192.168.5.2:4001",
    "connect EHOSTUNREACH 10.0.0.5:4001",
    "connect ENETUNREACH 192.168.5.2:4001",
    "connect ENOTFOUND host.docker.internal",
    "connect EADDRNOTAVAIL 192.168.5.2:4001",
    "connect EAI_AGAIN host.docker.internal",
  ])("classifies %s as a recoverable connection issue", (msg) => {
    expect(classifyIBConnectionError(msg)?.code).toBe(IB_MFA_REQUIRED_ISSUE);
  });

  it("ignores unrelated IB status messages", () => {
    const issue = classifyIBConnectionError(
      "Market data farm connection is OK:usopt",
      {
        ibHost: "127.0.0.1",
        ibPort: 4001,
      },
    );
    expect(issue).toBeNull();
  });

  it("ignores empty / nullish messages", () => {
    expect(classifyIBConnectionError("")).toBeNull();
    expect(classifyIBConnectionError(undefined)).toBeNull();
    expect(classifyIBConnectionError(null)).toBeNull();
  });
});
