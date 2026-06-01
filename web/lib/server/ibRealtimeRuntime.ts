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
    const data = JSON.parse(
      readFileSync(path, "utf8"),
    ) as IbRealtimeRuntimeFile;
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

function stripHostPort(host: string | null | undefined): string | null {
  if (!host) return null;
  const h = host.trim();
  if (!h) return null;
  let bare: string;
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    bare = end === -1 ? h : h.slice(0, end + 1);
  } else {
    const colon = h.indexOf(":");
    bare = colon === -1 ? h : h.slice(0, colon);
  }
  if (bare === "0.0.0.0" || bare === "::" || bare === "[::]") return null;
  return bare;
}

export function resolveBrowserIbRealtimeWsUrl({
  envUrl = process.env.NEXT_PUBLIC_IB_REALTIME_WS_URL,
  runtimeFile,
  host,
  forwardedHost,
  forwardedProto,
  defaultPort = DEFAULT_IB_REALTIME_PORT,
}: {
  envUrl?: string;
  runtimeFile?: string;
  host?: string | null;
  forwardedHost?: string | null;
  forwardedProto?: string | null;
  defaultPort?: number;
}): string {
  if (envUrl) return envUrl;
  const runtime = readIbRealtimeRuntimeFile(runtimeFile);
  const port = runtime?.port ?? defaultPort;
  const hostNoPort = stripHostPort(forwardedHost) ?? stripHostPort(host);
  if (!hostNoPort) return `ws://127.0.0.1:${port}`;
  const protocol = forwardedProto === "https" ? "wss:" : "ws:";
  return `${protocol}//${hostNoPort}:${port}`;
}
