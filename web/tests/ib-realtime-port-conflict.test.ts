import { afterEach, describe, expect, it } from "vitest";
import http from "node:http";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = resolve(fileURLToPath(import.meta.url), "..");
const webDir = resolve(__dirname, "..");
const projectRoot = resolve(webDir, "..");
const serverScript = resolve(
  projectRoot,
  "scripts",
  "infra",
  "ib_realtime",
  "ib_realtime_server.js",
);

const occupiedServers: http.Server[] = [];

afterEach(async () => {
  while (occupiedServers.length > 0) {
    const server = occupiedServers.pop();
    if (!server) continue;
    await new Promise<void>((resolveClose, rejectClose) => {
      server.close((error) => {
        if (error) {
          rejectClose(error);
          return;
        }
        resolveClose();
      });
    });
  }
});

async function occupyPort() {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("not xenon");
  });
  occupiedServers.push(server);
  server.listen(0, "0.0.0.0");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Expected TCP address info for occupied test port");
  }
  return address.port;
}

describe("ib realtime server startup", () => {
  it(
    "moves to a fallback port when the default port is occupied by a non-Xenon service",
    { timeout: 15_000 },
    async () => {
      const port = await occupyPort();
      const runtimeDir = mkdtempSync(join(tmpdir(), "xenon-ib-realtime-"));
      const runtimeFile = join(runtimeDir, "runtime.json");

      const child = spawn(
        process.execPath,
        [serverScript, "--port", String(port)],
        {
          cwd: projectRoot,
          env: {
            ...process.env,
            IB_REALTIME_RUNTIME_FILE: runtimeFile,
          },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => {
        stdout += chunk.toString();
      });
      child.stderr.on("data", (chunk) => {
        stderr += chunk.toString();
      });

      await waitFor(
        () =>
          stdout.includes("occupied by a non-Xenon service") &&
          stdout.includes("using fallback port") &&
          stdout.includes("WebSocket server listening on"),
        10_000,
      );

      child.kill("SIGTERM");
      const [code] = await once(child, "exit");

      expect(stdout).toContain(
        `WebSocket port already in use at ws://0.0.0.0:${port}`,
      );
      expect(stdout).toContain("occupied by a non-Xenon service");
      expect(stdout).toContain("using fallback port");
      expect(code).toBe(0);
      expect(stderr).not.toContain("EADDRINUSE");
    },
  );
});

async function waitFor(check: () => boolean, timeoutMs: number) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (check()) return;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 50));
  }
  throw new Error("Timed out waiting for child process output");
}
