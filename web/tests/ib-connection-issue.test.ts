import { describe, expect, it } from "vitest";
import {
  IB_MFA_REQUIRED_ISSUE,
  classifyIBConnectionError,
} from "../../scripts/ib_connection_status.js";

describe("classifyIBConnectionError", () => {
  it("treats a local gateway ECONNREFUSED as an MFA approval issue", () => {
    const issue = classifyIBConnectionError("connect ECONNREFUSED 127.0.0.1:4001", {
      ibHost: "127.0.0.1",
      ibPort: 4001,
    });

    expect(issue).toEqual(expect.objectContaining({
      code: IB_MFA_REQUIRED_ISSUE,
    }));
    expect(issue?.operatorMessage).toMatch(/Interactive Brokers/i);
    expect(issue?.operatorMessage).toMatch(/push notification/i);
    expect(issue?.operatorMessage).toMatch(/phone/i);
  });

  it("ignores unrelated IB errors", () => {
    const issue = classifyIBConnectionError("Market data farm connection is OK:usopt", {
      ibHost: "127.0.0.1",
      ibPort: 4001,
    });

    expect(issue).toBeNull();
  });
});
