import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { join } from "path";

const TEST_DIR = fileURLToPath(new URL(".", import.meta.url));
const HEADER_SOURCE = readFileSync(join(TEST_DIR, "../components/Header.tsx"), "utf-8");
const SHELL_SOURCE = readFileSync(join(TEST_DIR, "../components/WorkspaceShell.tsx"), "utf-8");

describe("Header fullscreen control", () => {
  it("adds a dedicated fullscreen button next to the theme toggle", () => {
    expect(HEADER_SOURCE).toContain("onToggleFullscreen");
    expect(HEADER_SOURCE).toContain("isFullscreen");
    expect(HEADER_SOURCE).toContain("Enter fullscreen");
    expect(HEADER_SOURCE).toContain("Exit fullscreen");
  });

  it("uses maximize/minimize icons for fullscreen state", () => {
    expect(HEADER_SOURCE).toMatch(/Maximize2|Minimize2/);
  });
});

describe("WorkspaceShell fullscreen behavior", () => {
  it("requests fullscreen on the document root and exits fullscreen on Escape", () => {
    expect(SHELL_SOURCE).toContain("requestFullscreen");
    expect(SHELL_SOURCE).toContain("document.exitFullscreen");
    expect(SHELL_SOURCE).toContain('event.key === "Escape"');
    expect(SHELL_SOURCE).toContain("fullscreenchange");
  });
});
