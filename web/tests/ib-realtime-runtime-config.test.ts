import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  readIbRealtimeRuntimeFile,
  resolveBrowserIbRealtimeWsUrl,
  resolveServerIbRealtimeWsUrl,
} from "@/lib/server/ibRealtimeRuntime";

const tempDirs: string[] = [];

afterEach(() => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (!dir) continue;
    rmSync(dir, { recursive: true, force: true });
  }
});

function writeRuntimeFile(port: number) {
  const dir = mkdtempSync(join(tmpdir(), "xenon-ib-realtime-runtime-"));
  tempDirs.push(dir);
  const runtimeFile = join(dir, "runtime.json");
  writeFileSync(
    runtimeFile,
    JSON.stringify({
      port,
      pid: process.pid,
      started_at: new Date().toISOString(),
    }),
    "utf8",
  );
  return runtimeFile;
}

describe("ib realtime runtime config", () => {
  it("prefers the runtime file for server-side websocket URL resolution", () => {
    const runtimeFile = writeRuntimeFile(8876);

    expect(readIbRealtimeRuntimeFile(runtimeFile)).toMatchObject({
      port: 8876,
    });
    expect(
      resolveServerIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile,
      }),
    ).toBe("ws://127.0.0.1:8876");
  });

  it("lets an explicit env URL override the runtime file", () => {
    const runtimeFile = writeRuntimeFile(8876);

    expect(
      resolveServerIbRealtimeWsUrl({
        envUrl: "ws://quotes.internal:9001",
        runtimeFile,
      }),
    ).toBe("ws://quotes.internal:9001");
  });
});

describe("resolveBrowserIbRealtimeWsUrl (header-derived)", () => {
  it("builds the URL from the runtime-file port and the Host header", () => {
    const runtimeFile = writeRuntimeFile(8876);
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile,
        host: "localhost:3000",
      }),
    ).toBe("ws://localhost:8876");
  });

  it("uses the default port and the Tailscale host", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "mini.tailnet.ts.net:3000",
      }),
    ).toBe("ws://mini.tailnet.ts.net:8765");
  });

  it("prefers x-forwarded-host over host and uses wss for https", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "internal:3000",
        forwardedHost: "edge.example.com",
        forwardedProto: "https",
      }),
    ).toBe("wss://edge.example.com:8765");
  });

  it("preserves IPv6 host literals", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "[::1]:3000" }),
    ).toBe("ws://[::1]:8765");
  });

  it("falls back to loopback when no host header is present", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: null }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("rejects literal 0.0.0.0 and falls back to loopback", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "0.0.0.0:3000",
      }),
    ).toBe("ws://127.0.0.1:8765");
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "trusted:3000",
        forwardedHost: "0.0.0.0",
      }),
    ).toBe("ws://trusted:8765");
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "0.0.0.0:3000",
        forwardedHost: "0.0.0.0",
      }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("treats an empty host string as missing (loopback fallback)", () => {
    expect(resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "" })).toBe(
      "ws://127.0.0.1:8765",
    );
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "   " }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("lets an explicit env URL override everything", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: "ws://quotes.internal:9001",
        host: "x:3000",
      }),
    ).toBe("ws://quotes.internal:9001");
  });
});
