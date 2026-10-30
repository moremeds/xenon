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

    expect(readIbRealtimeRuntimeFile(runtimeFile)).toMatchObject({ port: 8876 });
    expect(
      resolveServerIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile,
      }),
    ).toBe("ws://127.0.0.1:8876");
  });

  it("builds a browser-safe websocket URL from the runtime file and request host", () => {
    const runtimeFile = writeRuntimeFile(8876);

    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile,
        requestUrl: "http://localhost:3000/api/ib/ws-config",
      }),
    ).toBe("ws://localhost:8876");
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
