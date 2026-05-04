import { existsSync, readFileSync } from "fs";
import { join } from "path";
import { describe, expect, it } from "vitest";

const sourcePath = existsSync(join(process.cwd(), "web/components/PositionTable.tsx"))
  ? join(process.cwd(), "web/components/PositionTable.tsx")
  : join(process.cwd(), "components/PositionTable.tsx");
const source = readFileSync(sourcePath, "utf8");

describe("PositionTable position-rules wiring", () => {
  it("renders a protection column from caller-provided positionRules", () => {
    expect(source).toContain("positionRules?: PositionRule[]");
    expect(source).toContain("rulesForPortfolioPosition(pos, positionRules");
    expect(source).toContain("<th>Protection</th>");
    expect(source).toContain("<ShieldBadge");
  });
});
