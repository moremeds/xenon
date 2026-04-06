import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";

type HarnessMode = "external" | "spawned" | "unavailable";

type HealthPayload = {
  status?: string;
  test_mode?: boolean;
};

export type FastApiHarness = {
  available: boolean;
  baseUrl: string;
  mode: HarnessMode;
  skipReason?: string;
  close: () => Promise<void>;
};

const PROJECT_ROOT = new URL("../../", import.meta.url);
const DEFAULT_HOST = "127.0.0.1";
const HEALTH_TIMEOUT_MS = 1_500;
const START_TIMEOUT_MS = 20_000;

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return Promise.race([
    promise,
    delay(timeoutMs).then(() => {
      throw new Error(`Timed out after ${timeoutMs}ms`);
    }),
  ]);
}

async function fetchHealth(baseUrl: string): Promise<HealthPayload | null> {
  try {
    const res = await withTimeout(fetch(`${baseUrl}/health`), HEALTH_TIMEOUT_MS);
    if (!res.ok) return null;
    return (await res.json()) as HealthPayload;
  } catch {
    return null;
  }
}

async function isReusableTestServer(baseUrl: string): Promise<boolean> {
  const payload = await fetchHealth(baseUrl);
  return payload?.status === "ok" && payload.test_mode === true;
}

async function reservePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, DEFAULT_HOST, () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => reject(new Error("Failed to reserve test FastAPI port")));
        return;
      }
      const { port } = address;
      server.close((error) => {
        if (error) reject(error);
        else resolve(port);
      });
    });
  });
}

async function waitForHealth(baseUrl: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isReusableTestServer(baseUrl)) {
      return true;
    }
    await delay(250);
  }
  return false;
}

async function stopProcess(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode != null || child.killed) return;
  child.kill("SIGTERM");
  const exited = await Promise.race([
    new Promise<boolean>((resolve) => child.once("exit", () => resolve(true))),
    delay(2_000).then(() => false),
  ]);
  if (!exited && child.exitCode == null) {
    child.kill("SIGKILL");
    await new Promise<void>((resolve) => child.once("exit", () => resolve()));
  }
}

export async function ensureTestFastApi(): Promise<FastApiHarness> {
  const explicitBaseUrl = process.env.RADON_TEST_FASTAPI_URL ?? process.env.RADON_API_URL;
  if (explicitBaseUrl && await isReusableTestServer(explicitBaseUrl)) {
    process.env.RADON_API_URL = explicitBaseUrl;
    return {
      available: true,
      baseUrl: explicitBaseUrl,
      mode: "external",
      close: async () => {},
    };
  }

  const port = await reservePort();
  const baseUrl = `http://${DEFAULT_HOST}:${port}`;
  const child = spawn(
    "python3.13",
    [
      "-m",
      "uvicorn",
      "scripts.api.server:app",
      "--host",
      DEFAULT_HOST,
      "--port",
      String(port),
      "--app-dir",
      ".",
    ],
    {
      cwd: PROJECT_ROOT,
      env: {
        ...process.env,
        RADON_API_TEST_MODE: "1",
        PYTHONUNBUFFERED: "1",
      },
      stdio: "pipe",
    },
  );

  let stderrLog = "";
  child.stderr.on("data", (chunk) => {
    stderrLog += chunk.toString();
  });

  const healthy = await waitForHealth(baseUrl, START_TIMEOUT_MS);
  if (!healthy) {
    await stopProcess(child);
    return {
      available: false,
      baseUrl,
      mode: "unavailable",
      skipReason: stderrLog.trim() || "Failed to start test FastAPI server",
      close: async () => {},
    };
  }

  process.env.RADON_API_URL = baseUrl;
  return {
    available: true,
    baseUrl,
    mode: "spawned",
    close: async () => {
      await stopProcess(child);
    },
  };
}
