import { defineConfig } from "vitest/config";
import { resolve } from "path";

export default defineConfig({
  // Repo root so `web/tests/**` includes match when `npm run test` runs from `web/`.
  root: resolve(__dirname),
  resolve: {
    alias: {
      "@tools": resolve(__dirname, "lib/tools"),
      "@/lib": resolve(__dirname, "web/lib"),
      "@": resolve(__dirname, "web"),
    },
  },
  test: {
    include: [
      "lib/tools/__tests__/**/*.test.ts",
      "web/tests/**/*.test.ts",
      "web/tests/**/*.test.tsx",
    ],
    globalSetup: ["web/tests/setup/seed-fixtures.ts"],
    environment: "node",
    coverage: {
      provider: "v8",
      include: [
        "web/lib/**/*.ts",
        "web/app/api/**/*.ts",
        "lib/tools/**/*.ts",
      ],
      exclude: [
        "**/*.test.ts",
        "**/node_modules/**",
        "web/lib/use*.ts", // React hooks need jsdom
        "web/lib/OrderActionsContext.tsx",
        "web/lib/TickerDetailContext.tsx",
        "web/lib/types.ts", // Pure type definitions, no runtime code
        "lib/tools/wrappers/ib-order-manage.ts", // Thin runScript wrapper
        "lib/tools/wrappers/ib-orders.ts", // Thin runScript wrapper
        "web/app/api/prices/**", // WebSocket client, needs live IB server
        "web/app/api/blotter/**", // Spawns Python subprocess for Flex Query
      ],
    },
  },
});
