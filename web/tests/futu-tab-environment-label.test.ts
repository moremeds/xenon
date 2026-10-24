import { describe, expect, it } from "vitest";
import { futuTabEnvironmentLabel } from "@/lib/futuAccountEnv";

describe("futuTabEnvironmentLabel", () => {
  it("maps paper to simulate and live to real", () => {
    expect(futuTabEnvironmentLabel("paper")).toBe("simulate");
    expect(futuTabEnvironmentLabel("live")).toBe("real");
  });
});
