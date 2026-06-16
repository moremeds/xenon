import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  classifyIBConnectionError,
  isInfoCode,
  isDepthPermissionError,
} from "../../../scripts/infra/ib_realtime/ib_connection_status.js";

describe("ib_connection_status — @stoqey/ib error/info split", () => {
  it("treats data-farm-OK codes as info (must not drop ib_connected)", () => {
    for (const code of [2103, 2104, 2106, 2108, 2158]) {
      expect(isInfoCode(code)).toBe(true);
    }
  });

  it("treats real fault codes as non-info", () => {
    for (const code of [1100, 504, 502, 200, 354, 10182]) {
      expect(isInfoCode(code)).toBe(false);
    }
  });

  it("classifies socket connect failures as the MFA/reconnect issue", () => {
    const r = classifyIBConnectionError(
      "connect ECONNREFUSED 192.168.5.2:4001",
    );
    expect(r?.code).toBe("ibc_mfa_required");
  });

  it("returns null for non-socket text (informational / app errors)", () => {
    expect(
      classifyIBConnectionError("Market data farm connection is OK:usfarm"),
    ).toBeNull();
    expect(classifyIBConnectionError("No security definition")).toBeNull();
  });
});

describe("ib_connection_status — depth permission classification", () => {
  // Only GENUINE no-entitlement signals should tear down a depth/tape ticket.
  it("flags real no-entitlement codes + messages", () => {
    expect(isDepthPermissionError(10089, "")).toBe(true);
    expect(isDepthPermissionError(10092, "")).toBe(true);
    expect(
      isDepthPermissionError(
        0,
        "Market depth is not allowed for this exchange",
      ),
    ).toBe(true);
    expect(isDepthPermissionError(0, "Market depth data is not eligible")).toBe(
      true,
    );
    expect(
      isDepthPermissionError(0, "not supported for this combination"),
    ).toBe(true);
  });

  // Regression for the freeze: IB emits these on an ENTITLED book mid-stream
  // (2152 = undocumented 21xx system warning seen live on QQQ; 317 = depth
  // RESET; 316 = halted; 309 = max depth lines). Tearing down on any of them
  // killed a working ladder after a few seconds. They are NOT entitlement loss.
  it("does NOT flag mid-stream operational depth warnings", () => {
    for (const code of [2152, 316, 317, 309]) {
      expect(isDepthPermissionError(code, "")).toBe(false);
    }
  });
});

// Source-level guard: the @stoqey migration must NOT drop the error-triage
// branches that the old (error, data) handler carried. Cheap regression net
// for the clientId-rotation + info-regex logic that a live boot would exercise.
describe("relay error handler preserves triage post-migration", () => {
  const __dirname = resolve(fileURLToPath(import.meta.url), "..");
  const source = readFileSync(
    resolve(
      __dirname,
      "..",
      "..",
      "..",
      "scripts",
      "infra",
      "ib_realtime",
      "ib_realtime_server.js",
    ),
    "utf8",
  );

  it("registers the error handler on EventName.error with the (error, code, reqId) signature", () => {
    expect(source).toMatch(
      /ib\.on\(\s*EventName\.error,\s*\(error,\s*code,\s*reqId\)\s*=>/,
    );
  });

  it("keeps the clientId-in-use rotation branch", () => {
    expect(source).toContain("/client id is already in use/i");
    expect(source).toContain("rotateIBClient(");
  });

  it("keeps the farm-connection-OK info regex as a fallback filter", () => {
    expect(source).toMatch(/farm connection is OK/i);
  });

  it("short-circuits info codes before the triage chain", () => {
    expect(source).toMatch(/if \(isInfoCode\(code\)\)/);
  });

  it("registers a separate EventName.info channel", () => {
    expect(source).toMatch(/ib\.on\(\s*EventName\.info,/);
  });

  it("gates depth/tape teardown behind the narrow permission check (no broad no-entitlement)", () => {
    // The freeze regressed because ANY depth/tape-scoped code emitted
    // no-entitlement + teardown. The handler must route that decision through
    // isDepthPermissionError so operational warnings (2152/317) never kill a
    // live ticket.
    expect(source).toContain("isDepthPermissionError(");
  });
});
