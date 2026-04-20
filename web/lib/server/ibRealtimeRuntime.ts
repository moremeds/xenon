import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

type IbRealtimeRuntimeFile = {
  port: number;
  pid?: number;
  started_at?: string;
};

const DEFAULT_IB_REALTIME_PORT = 8765;

export function getIbRealtimeRuntimeFilePath(runtimeFile?: string): string {
  return (
    runtimeFile ??
    process.env.IB_REALTIME_RUNTIME_FILE ??
    join(tmpdir(), "xenon-ib-realtime.json")
  );
}

export function readIbRealtimeRuntimeFile(
  runtimeFile?: string,
): IbRealtimeRuntimeFile | null {
  const path = getIbRealtimeRuntimeFilePath(runtimeFile);
  if (!existsSync(path)) return null;

  try {
    const data = JSON.parse(readFileSync(path, "utf8")) as IbRealtimeRuntimeFile;
    if (!Number.isInteger(data.port) || data.port <= 0) return null;
    return data;
  } catch {
    return null;
  }
}

export function resolveServerIbRealtimeWsUrl({
  envUrl = process.env.IB_REALTIME_WS_URL,
  runtimeFile,
  defaultPort = DEFAULT_IB_REALTIME_PORT,
}: {
  envUrl?: string;
  runtimeFile?: string;
  defaultPort?: number;
} = {}): string {
  if (envUrl) return envUrl;
  const runtime = readIbRealtimeRuntimeFile(runtimeFile);
  const port = runtime?.port ?? defaultPort;
  return `ws://127.0.0.1:${port}`;
}

export function resolveBrowserIbRealtimeWsUrl({
  envUrl = process.env.NEXT_PUBLIC_IB_REALTIME_WS_URL,
  runtimeFile,
  requestUrl,
  defaultPort = DEFAULT_IB_REALTIME_PORT,
}: {
  envUrl?: string;
  runtimeFile?: string;
  requestUrl: string;
  defaultPort?: number;
}): string {
  if (envUrl) return envUrl;
  const runtime = readIbRealtimeRuntimeFile(runtimeFile);
  const port = runtime?.port ?? defaultPort;
  const url = new URL(requestUrl);
  const protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${url.hostname}:${port}`;
}
