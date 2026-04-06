import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = resolve(fileURLToPath(import.meta.url), "..");
const webDir = resolve(__dirname, "..");
const projectRoot = resolve(webDir, "..");
const source = readFileSync(resolve(projectRoot, "scripts", "ib_realtime_server.js"), "utf8");

describe("ib_realtime_server.js localhost auth bypass", () => {
  it("skips ticket validation for localhost connections", () => {
    // The upgrade handler must bypass auth for localhost remoteAddress
    expect(source).toContain('remoteAddr === "127.0.0.1"');
    expect(source).toContain('remoteAddr === "::1"');
    expect(source).toContain('remoteAddr === "::ffff:127.0.0.1"');
  });

  it("still validates tickets for non-localhost when CLERK_JWKS_URL is set", () => {
    // Ticket validation block must exist after the localhost bypass
    expect(source).toContain('const ticket = url.searchParams.get("ticket")');
    expect(source).toContain("TICKET_VALIDATE_URL");
    expect(source).toContain("401 Unauthorized");
  });
});

describe("ib_realtime_server.js stale-data restart modes", () => {
  it("keeps cloud and docker on reconnect-only recovery", () => {
    const cloudDockerBlock = source.match(
      /if \(GATEWAY_MODE === "cloud" \|\| GATEWAY_MODE === "docker"\) \{[\s\S]*?\n  \} else \{/,
    )?.[0] ?? "";

    expect(cloudDockerBlock).toContain('GATEWAY_MODE === "cloud" || GATEWAY_MODE === "docker"');
    expect(cloudDockerBlock).toContain("ib.disconnect()");
    expect(cloudDockerBlock).toContain("scheduleReconnect();");
    expect(cloudDockerBlock).not.toContain("restart-secure-ibc-service.sh");
  });

  it("uses ESM imports for launchd restarts instead of require()", () => {
    const launchdBlock = source.match(/else \{\n    \/\/ LaunchD mode — shell out to restart IBC service[\s\S]*?\n  \}/)?.[0] ?? "";

    expect(source).toContain('import { execSync } from "node:child_process";');
    expect(source).toContain('import { homedir } from "node:os";');
    expect(source).not.toContain('require("child_process")');
    expect(source).not.toContain('require("os")');
    expect(launchdBlock).toContain("execSync(`");
    expect(launchdBlock).toContain("homedir()");
  });
});
