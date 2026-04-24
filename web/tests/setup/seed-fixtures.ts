import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = resolve(__dirname, "..", "..", "..");

const FIXTURES: Record<string, object> = {
  "data/flex_token_config.json": {
    expires_at: "2099-01-01T00:00:00Z",
    renewal_url:
      "https://www.interactivebrokers.com/AccountManagement/AmAuthentication",
    breadcrumb: "Performance & Reports → Flex Queries → Configure",
    reminder_days: [30, 14, 7, 1],
  },
};

export default function setup(): void {
  for (const [rel, payload] of Object.entries(FIXTURES)) {
    const abs = resolve(ROOT, rel);
    if (existsSync(abs)) continue;
    mkdirSync(resolve(abs, ".."), { recursive: true });
    writeFileSync(abs, JSON.stringify(payload, null, 2), "utf-8");
  }
}
