import { afterAll, describe, expect, it } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  resolveServerIbRealtimeWsUrl,
  resolveBrowserIbRealtimeWsUrl,
} from "@/lib/server/ibRealtimeRuntime";

// The @stoqey/ib migration is UPSTREAM-only (relay ↔ IB Gateway). The downstream
// WS URL contract that every consumer depends on must stay frozen. This pins it.
const MISSING = "/nonexistent-runtime-file.json";

describe("WS-URL contract (frozen across the @stoqey/ib migration)", () => {
  it("server default resolves to loopback :8765", () => {
    expect(
      resolveServerIbRealtimeWsUrl({ envUrl: undefined, runtimeFile: MISSING }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("server honors IB_REALTIME_WS_URL override", () => {
    expect(resolveServerIbRealtimeWsUrl({ envUrl: "ws://relay:9000" })).toBe(
      "ws://relay:9000",
    );
  });

  it("browser falls back to forwarded host + default port", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile: MISSING,
        forwardedHost: "xenon.example.com",
        forwardedProto: "https",
      }),
    ).toBe("wss://xenon.example.com:8765");
  });

  // The REAL failure mode (Codex ISSUE-12): the relay may bind a non-default
  // port (fallback when 8765 is occupied) and write it to the runtime file;
  // consumers must follow that port, not a hardcoded 8765.
  describe("runtime-file / fallback-port chain", () => {
    const dir = mkdtempSync(join(tmpdir(), "xenon-wsurl-"));
    const runtimeFile = join(dir, "xenon-ib-realtime.json");
    writeFileSync(runtimeFile, JSON.stringify({ port: 8866, pid: 1 }));
    afterAll(() => rmSync(dir, { recursive: true, force: true }));

    it("server resolves to the runtime-file port", () => {
      expect(
        resolveServerIbRealtimeWsUrl({ envUrl: undefined, runtimeFile }),
      ).toBe("ws://127.0.0.1:8866");
    });

    it("browser resolves to the runtime-file port (follows the actual bind)", () => {
      expect(
        resolveBrowserIbRealtimeWsUrl({
          envUrl: undefined,
          runtimeFile,
          forwardedHost: "xenon.example.com",
          forwardedProto: "https",
        }),
      ).toBe("wss://xenon.example.com:8866");
    });
  });
});
