import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export type ScriptSuccess<T> = { ok: true; data: T };
export type ScriptFailure = {
  ok: false;
  exitCode: number | null;
  stderr: string;
};
export type ScriptResult<T> = ScriptSuccess<T> | ScriptFailure;

export interface RunScriptOptions {
  args?: string[];
  cwd?: string;
  timeout?: number;
  maxOutput?: number;
  rawOutput?: boolean;
}

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(MODULE_DIR, "..", "..", "..");

export function resolveProjectRoot(): string {
  if (
    existsSync(path.join(PROJECT_ROOT, "scripts")) &&
    existsSync(path.join(PROJECT_ROOT, "data"))
  ) {
    return PROJECT_ROOT;
  }
  return process.cwd();
}

export function runScript<T = unknown>(
  scriptPath: string,
  options: RunScriptOptions = {},
): Promise<ScriptResult<T>> {
  const root = options.cwd ?? resolveProjectRoot();
  const child = spawn(scriptPath, options.args ?? [], {
    cwd: root,
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });

  return collectResult<T>(child, options);
}

function collectResult<T>(
  child: ChildProcess,
  options: RunScriptOptions,
): Promise<ScriptResult<T>> {
  const maxOutput = options.maxOutput ?? 200_000;
  let stdout = "";
  let stderr = "";
  let timedOut = false;

  const timeout =
    options.timeout == null
      ? undefined
      : setTimeout(() => {
          timedOut = true;
          child.kill("SIGKILL");
        }, options.timeout);

  child.stdout?.on("data", (chunk) => {
    stdout = (stdout + String(chunk)).slice(-maxOutput);
  });
  child.stderr?.on("data", (chunk) => {
    stderr = (stderr + String(chunk)).slice(-maxOutput);
  });

  return new Promise((resolve) => {
    child.on("error", (error) => {
      if (timeout) clearTimeout(timeout);
      resolve({ ok: false, exitCode: null, stderr: error.message });
    });

    child.on("close", (exitCode) => {
      if (timeout) clearTimeout(timeout);

      if (exitCode !== 0 || timedOut) {
        resolve({
          ok: false,
          exitCode,
          stderr: timedOut ? `${stderr}\nTimed out`.trim() : stderr,
        });
        return;
      }

      if (options.rawOutput) {
        resolve({ ok: true, data: stdout as T });
        return;
      }

      try {
        resolve({ ok: true, data: JSON.parse(stdout) as T });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        resolve({ ok: false, exitCode: 0, stderr: message });
      }
    });
  });
}
