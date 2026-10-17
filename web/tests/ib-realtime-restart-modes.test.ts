import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = resolve(fileURLToPath(import.meta.url), "..");
const webDir = resolve(__dirname, "..");
const projectRoot = resolve(webDir, "..");
const source = readFileSync(
  resolve(
    projectRoot,
    "scripts",
    "infra",
    "ib_realtime",
    "ib_realtime_server.js",
  ),
  "utf8",
);

describe("ib_realtime_server.js upgrade auth", () => {
  it("never bypasses ticket validation on peer address", () => {
    // Reachability is not authentication — no loopback shortcut may exist in
    // the upgrade handler.
    const upgradeBlock =
      source.match(/httpServer\.on\("upgrade"[\s\S]*?\n\}\);/)?.[0] ?? "";
    expect(upgradeBlock).not.toContain('remoteAddr === "127.0.0.1"');
    expect(upgradeBlock).not.toContain('remoteAddr === "::1"');
    expect(upgradeBlock).not.toContain('remoteAddr === "::ffff:127.0.0.1"');
    expect(upgradeBlock).toContain("authorizeUpgrade(req)");
  });

  it("validates every ticket via the FastAPI endpoint", () => {
    // Missing/foreign/expired tickets are rejected inside ws_upgrade_auth.js;
    // the handler delegates and destroys the socket on non-ok decisions.
    expect(source).toContain("authorizeUpgrade");
    expect(source).toContain("401 Unauthorized");
    expect(source).toContain("403 Forbidden");
  });
});

describe("ib_realtime_server.js stale-data restart modes", () => {
  it("keeps cloud and docker on reconnect-only recovery", () => {
    const cloudDockerBlock =
      source.match(
        /if \(GATEWAY_MODE === "cloud" \|\| GATEWAY_MODE === "docker"\) \{[\s\S]*?\n  \} else \{/,
      )?.[0] ?? "";

    expect(cloudDockerBlock).toContain(
      'GATEWAY_MODE === "cloud" || GATEWAY_MODE === "docker"',
    );
    expect(cloudDockerBlock).toContain("ib.disconnect()");
    expect(cloudDockerBlock).toContain("scheduleReconnect();");
    expect(cloudDockerBlock).not.toContain("restart-secure-ibc-service.sh");
  });

  it("uses ESM imports for launchd restarts instead of require()", () => {
    const launchdBlock =
      source.match(
        /else \{\n    \/\/ LaunchD mode — shell out to restart IBC service[\s\S]*?\n  \}/,
      )?.[0] ?? "";

    // Allow additional named imports from these modules (e.g. tmpdir alongside
    // homedir) — the contract is "ESM import, not require()", not an exact
    // import list.
    expect(source).toMatch(
      /import \{[^}]*\bexecSync\b[^}]*\} from "node:child_process";/,
    );
    expect(source).toMatch(/import \{[^}]*\bhomedir\b[^}]*\} from "node:os";/);
    expect(source).not.toContain('require("child_process")');
    expect(source).not.toContain('require("os")');
    expect(launchdBlock).toContain("execSync(`");
    expect(launchdBlock).toContain("homedir()");
  });
});
